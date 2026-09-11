//! What the venue task sends first, and what it refuses to send at all.
//!
//! One mutation is in flight at a time. These drive [`VenueClient`] directly,
//! so what is asserted is the worker's own choice among the commands already
//! waiting — not the engine's admission.

use super::*;

use crate::venue_runtime::{
    lane_full, queued_send_ids, selectable, selectable_by_scan, send_class, Command, DispatchClass,
    MutationCompletion, Queued, VenueClient, COMMAND_CAPACITY, COMPLETION_CAPACITY,
    MAX_REQUESTS_PER_COMMAND, READY_ORDINARY_CAPACITY, URGENT_CAPACITY,
};
use engine_types::{AuthorityEpoch, CommandAuthority};
use tokio::sync::mpsc;

/// A send slow enough that a test can queue behind it: under paused time the
/// worker parks in this sleep until the test itself awaits.
const HELD: Duration = Duration::from_millis(50);

fn request(id: &str, reduce_only: bool) -> OrderRequest {
    OrderRequest {
        client_order_id: id.into(),
        strategy: StrategyId(0),
        symbol: SymbolId(0),
        side: Side::Sell,
        qty: 0.1,
        kind: OrderKind::Market,
        stop: None,
        reduce_only,
        close_position: false,
        sleeve_effect: None,
        exact_terms: None,
    }
}

fn live(epoch: &AuthorityEpoch) -> CommandAuthority {
    CommandAuthority {
        epoch: epoch.current(),
        queued_ns: clock::now_ns(),
        expires_at_ns: u64::MAX,
    }
}

fn steps(tape: &Tape) -> Vec<Step> {
    tape.lock().unwrap().clone()
}

fn position(tape: &[Step], step: &Step) -> usize {
    tape.iter()
        .position(|seen| seen == step)
        .unwrap_or_else(|| panic!("{step:?} never reached the venue; tape was {tape:?}"))
}

/// Occupy the worker with one send and leave it parked inside the gateway.
async fn holding_worker() -> (VenueClient, mpsc::Receiver<MutationCompletion>, Tape) {
    let (mut venue, _) = MockVenue::new(tape(), &["BTCUSDT"]);
    venue.send_delay = HELD;
    let tape = venue.tape.clone();
    let (mut client, completions) = VenueClient::spawn(venue, AuthorityEpoch::new());
    client
        .dispatch_orders(vec![request("holding-send", true)], None)
        .unwrap();
    tokio::task::yield_now().await;
    assert_eq!(
        steps(&tape),
        vec![Step::Send("holding-send".into())],
        "the worker was not inside the gateway call"
    );
    (client, completions, tape)
}

async fn drain(completions: &mut mpsc::Receiver<MutationCompletion>, count: usize) {
    for _ in 0..count {
        tokio::time::timeout(Duration::from_secs(1), completions.recv())
            .await
            .expect("a queued command was never answered")
            .unwrap();
    }
}

#[test]
fn a_batch_is_risk_reducing_only_when_every_request_reduces_the_physical_position() {
    assert_eq!(
        send_class(&[request("a", true), request("b", true)]),
        DispatchClass::RiskReducing
    );
    assert_eq!(
        send_class(&[request("a", true), request("b", false)]),
        DispatchClass::Opening,
        "a batch mixing openings and reductions is an opening"
    );
    assert_eq!(send_class(&[]), DispatchClass::Opening);
}

#[tokio::test(start_paused = true)]
async fn a_queued_protective_cancel_is_sent_before_unsent_openings_once_the_current_send_releases()
{
    let (mut client, mut completions, tape) = holding_worker().await;
    let epoch = AuthorityEpoch::new();
    client
        .dispatch_orders(vec![request("opening-a", false)], Some(live(&epoch)))
        .unwrap();
    client
        .dispatch_cancels(vec![(SymbolId(0), "working-b".into())])
        .unwrap();
    drain(&mut completions, 3).await;

    let tape = steps(&tape);
    assert!(
        position(&tape, &Step::Cancel("working-b".into()))
            < position(&tape, &Step::Send("opening-a".into())),
        "an opening queued first delayed a protective cancel; tape was {tape:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn a_cancel_for_a_still_queued_send_waits_for_that_send_but_passes_other_openings() {
    let (mut client, mut completions, tape) = holding_worker().await;
    let epoch = AuthorityEpoch::new();
    client
        .dispatch_orders(vec![request("dependent-c", false)], Some(live(&epoch)))
        .unwrap();
    client
        .dispatch_orders(vec![request("other-d", false)], Some(live(&epoch)))
        .unwrap();
    client
        .dispatch_cancels(vec![(SymbolId(0), "dependent-c".into())])
        .unwrap();
    drain(&mut completions, 4).await;

    let tape = steps(&tape);
    let cancel = position(&tape, &Step::Cancel("dependent-c".into()));
    assert!(
        position(&tape, &Step::Send("dependent-c".into())) < cancel,
        "a cancel overtook the placement it names; tape was {tape:?}"
    );
    assert!(
        cancel < position(&tape, &Step::Send("other-d".into())),
        "waiting for its own send put the cancel behind unrelated openings; tape was {tape:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn a_reducing_order_and_a_cancel_ignore_authority_epoch_and_expiry() {
    let (venue, _) = MockVenue::new(tape(), &["BTCUSDT"]);
    let sends = venue.sends.clone();
    let cancels = venue.cancels.clone();
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    client
        .dispatch_orders(vec![request("protective-reduction", true)], None)
        .unwrap();
    client
        .dispatch_cancels(vec![(SymbolId(0), "pull-me".into())])
        .unwrap();
    // Everything an opening could lose, lost before either command is taken.
    epoch.advance();
    epoch.advance();
    drain(&mut completions, 2).await;

    assert_eq!(
        sends
            .lock()
            .unwrap()
            .iter()
            .map(|request| request.client_order_id.clone())
            .collect::<Vec<_>>(),
        vec!["protective-reduction".to_string()],
        "risk-off was refused at the send boundary"
    );
    assert_eq!(
        *cancels.lock().unwrap(),
        vec![(SymbolId(0), "pull-me".to_string())]
    );
}

#[tokio::test(start_paused = true)]
async fn a_superseded_opening_is_answered_without_a_venue_answer_or_a_quota_charge() {
    let (venue, _) = MockVenue::new(tape(), &["BTCUSDT"]);
    let sends = venue.sends.clone();
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    let authority = live(&epoch);
    epoch.advance();
    client
        .dispatch_orders(vec![request("superseded-opening", false)], Some(authority))
        .unwrap();
    let completion = tokio::time::timeout(Duration::from_secs(1), completions.recv())
        .await
        .expect("the refused opening was never answered")
        .unwrap();

    let MutationCompletion::Orders {
        started_ns,
        completed_ns,
        rate_wait_ns,
        replies,
        ..
    } = completion
    else {
        panic!("expected a placement completion");
    };
    assert_eq!(
        started_ns, completed_ns,
        "a refusal spent time at the venue"
    );
    assert_eq!(rate_wait_ns, None);
    assert!(
        matches!(&replies[0], Err(VenueError::BadRequest(reason))
            if reason == "authority: epoch 1 superseded by 2"),
        "{replies:?}"
    );
    assert!(sends.lock().unwrap().is_empty());
}

// ---------------------------------------------------- a venue with a quota

/// What the venue's own request quota is holding back, by lane. The query and
/// the call read the same state, as an adapter that paces itself does: what
/// the worker is told it would wait is what the call then waits out.
#[derive(Default)]
struct Quota {
    openings_free_at: Option<tokio::time::Instant>,
    stops_free_at: Option<tokio::time::Instant>,
    /// Amend requests the lane will still take before `amend_free_at`.
    amend_slots: usize,
    /// What the lane refills to, and how long after it runs dry. A zero
    /// window is a venue that does not pace amends at all.
    amend_window: usize,
    amend_refill: Duration,
    amend_free_at: Option<tokio::time::Instant>,
    queries: usize,
    /// What the gateway call itself waited out. Zero means the worker was
    /// free for the hold rather than parked inside the call.
    held_in_call: Duration,
}

impl Quota {
    fn refill_amends(&mut self) {
        if self
            .amend_free_at
            .is_some_and(|free_at| free_at <= tokio::time::Instant::now())
        {
            self.amend_slots = self.amend_window;
            self.amend_free_at = None;
        }
    }
}

struct QuotaVenue {
    marks: Arc<Mutex<Vec<String>>>,
    /// The client order ids of each `amend_orders_under` group that reached
    /// the venue, in call order.
    amend_calls: Arc<Mutex<Vec<Vec<String>>>>,
    quota: Arc<Mutex<Quota>>,
}

impl QuotaVenue {
    fn new() -> Self {
        QuotaVenue {
            marks: Arc::new(Mutex::new(Vec::new())),
            amend_calls: Arc::new(Mutex::new(Vec::new())),
            quota: Arc::new(Mutex::new(Quota::default())),
        }
    }

    fn hold_openings(&self, hold: Duration) {
        self.quota.lock().unwrap().openings_free_at = Some(tokio::time::Instant::now() + hold);
    }

    fn hold_stops(&self, hold: Duration) {
        self.quota.lock().unwrap().stops_free_at = Some(tokio::time::Instant::now() + hold);
    }

    /// An amend lane with `slots` free now, refilling to `window` `refill`
    /// after it empties.
    fn hold_amends(&self, slots: usize, window: usize, refill: Duration) {
        let mut quota = self.quota.lock().unwrap();
        quota.amend_slots = slots;
        quota.amend_window = window;
        quota.amend_refill = refill;
        quota.amend_free_at = (slots == 0).then(|| tokio::time::Instant::now() + refill);
    }

    fn spend_amends(&self, requests: usize) {
        let mut quota = self.quota.lock().unwrap();
        quota.refill_amends();
        quota.amend_slots = quota.amend_slots.saturating_sub(requests);
        if quota.amend_slots == 0 && quota.amend_window > 0 && quota.amend_free_at.is_none() {
            quota.amend_free_at = Some(tokio::time::Instant::now() + quota.amend_refill);
        }
    }

    fn remaining(&self, command: engine_types::QueuedCommand) -> Duration {
        let mut quota = self.quota.lock().unwrap();
        if let engine_types::QueuedCommand::Amend { requests } = command {
            quota.refill_amends();
            if requests <= quota.amend_slots {
                return Duration::ZERO;
            }
            return match quota.amend_free_at {
                Some(free_at) => free_at.saturating_duration_since(tokio::time::Instant::now()),
                None if quota.amend_window > 0 => quota.amend_refill,
                None => Duration::ZERO,
            };
        }
        let lane = match command {
            engine_types::QueuedCommand::Opening { .. } => quota.openings_free_at,
            engine_types::QueuedCommand::PositionStop => quota.stops_free_at,
            _ => None,
        };
        lane.map(|free_at| free_at.saturating_duration_since(tokio::time::Instant::now()))
            .unwrap_or_default()
    }

    async fn admit(&self, command: engine_types::QueuedCommand) {
        let wait = self.remaining(command);
        if !wait.is_zero() {
            self.quota.lock().unwrap().held_in_call += wait;
            tokio::time::sleep(wait).await;
        }
    }

    fn mark(&self, what: String) {
        self.marks.lock().unwrap().push(what);
    }
}

#[engine_types::async_trait]
impl VenueGateway for QuotaVenue {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            native_position_stop: true,
            amend_in_place: true,
            set_leverage: false,
            close_position_below_minimum: false,
        }
    }

    fn quota_wait(&self, command: engine_types::QueuedCommand) -> Duration {
        self.quota.lock().unwrap().queries += 1;
        self.remaining(command)
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        Ok(AccountIdentity {
            venue: "quota".into(),
            user_id: "7000001".into(),
            realm: "demo".into(),
        })
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        self.admit(if req.reduce_only {
            engine_types::QueuedCommand::Reducing { requests: 1 }
        } else {
            engine_types::QueuedCommand::Opening { requests: 1 }
        })
        .await;
        self.mark(format!("send {}", req.client_order_id));
        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id: format!("venue-{}", req.client_order_id),
            sent_ns: clock::now_ns(),
            ack_ns: clock::now_ns(),
        })
    }

    async fn cancel_order(&mut self, _symbol: SymbolId, id: &str) -> Result<(), VenueError> {
        self.admit(engine_types::QueuedCommand::Cancel { requests: 1 })
            .await;
        self.mark(format!("cancel {id}"));
        Ok(())
    }

    async fn amend_order(
        &mut self,
        symbol: SymbolId,
        id: &str,
        spec: AmendSpec,
    ) -> Result<(), VenueError> {
        self.amend_orders(&[(symbol, id.to_string(), spec)])
            .await
            .pop()
            .unwrap_or(Ok(()))
    }

    async fn amend_orders(
        &mut self,
        requests: &[(SymbolId, String, AmendSpec)],
    ) -> Vec<Result<(), VenueError>> {
        let ids: Vec<String> = requests.iter().map(|(_, id, _)| id.clone()).collect();
        self.admit(engine_types::QueuedCommand::Amend {
            requests: ids.len(),
        })
        .await;
        self.spend_amends(ids.len());
        for id in &ids {
            self.mark(format!("amend {id}"));
        }
        self.amend_calls.lock().unwrap().push(ids);
        requests.iter().map(|_| Ok(())).collect()
    }

    async fn set_stop(&mut self, symbol: SymbolId, _trigger_px: f64) -> Result<(), VenueError> {
        self.admit(engine_types::QueuedCommand::PositionStop).await;
        self.mark(format!("stop {}", symbol.0));
        Ok(())
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        Ok(AccountView {
            exact_amounts: None,
            equity_usdt: 10_000.0,
            available_usdt: 10_000.0,
            positions: Vec::new(),
            observed_ns: clock::now_ns(),
        })
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        Ok(Vec::new())
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        Ok(Vec::new())
    }
}

fn marks(venue: &QuotaVenue) -> Arc<Mutex<Vec<String>>> {
    venue.marks.clone()
}

fn seen(marks: &Arc<Mutex<Vec<String>>>) -> Vec<String> {
    marks.lock().unwrap().clone()
}

const HELD_BY_QUOTA: Duration = Duration::from_secs(2);

/// These wait out quota holds measured in seconds, so the bound on an answer
/// is theirs rather than [`drain`]'s.
async fn served(completions: &mut mpsc::Receiver<MutationCompletion>, count: usize) {
    for _ in 0..count {
        tokio::time::timeout(Duration::from_secs(30), completions.recv())
            .await
            .expect("a held command was never answered")
            .unwrap();
    }
}

#[tokio::test(start_paused = true)]
async fn an_opening_the_venue_quota_holds_back_does_not_occupy_the_worker() {
    let venue = QuotaVenue::new();
    venue.hold_openings(HELD_BY_QUOTA);
    let marks = marks(&venue);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    client
        .dispatch_orders(vec![request("opening-a", false)], Some(live(&epoch)))
        .unwrap();
    tokio::task::yield_now().await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    client
        .dispatch_cancels(vec![(SymbolId(0), "working-b".into())])
        .unwrap();

    let first = tokio::time::timeout(Duration::from_secs(1), completions.recv())
        .await
        .expect("the cancel waited behind the held opening")
        .unwrap();
    assert!(
        matches!(first, MutationCompletion::Cancels { .. }),
        "{first:?}"
    );
    assert_eq!(
        seen(&marks),
        vec!["cancel working-b".to_string()],
        "the opening reached the venue before its quota allowed it"
    );
}

#[tokio::test(start_paused = true)]
async fn a_held_opening_is_sent_once_its_quota_wait_elapses_and_only_once() {
    let venue = QuotaVenue::new();
    venue.hold_openings(HELD_BY_QUOTA);
    let marks = marks(&venue);
    let quota = venue.quota.clone();
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    let started = tokio::time::Instant::now();
    client
        .dispatch_orders(vec![request("opening-a", false)], Some(live(&epoch)))
        .unwrap();
    served(&mut completions, 1).await;

    assert_eq!(seen(&marks), vec!["send opening-a".to_string()]);
    assert_eq!(started.elapsed(), HELD_BY_QUOTA);
    assert_eq!(
        quota.lock().unwrap().held_in_call,
        Duration::ZERO,
        "the call waited the quota out, so the worker was parked inside it"
    );
}

#[tokio::test(start_paused = true)]
async fn a_wholly_held_ready_set_wakes_when_the_earliest_lane_frees() {
    let venue = QuotaVenue::new();
    venue.hold_openings(HELD_BY_QUOTA);
    venue.hold_stops(Duration::from_secs(1));
    let marks = marks(&venue);
    let queries = venue.quota.clone();
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    let started = tokio::time::Instant::now();
    client
        .dispatch_orders(vec![request("opening-a", false)], Some(live(&epoch)))
        .unwrap();
    client.dispatch_stop(SymbolId(0), 100.0, None).unwrap();
    tokio::task::yield_now().await;
    tokio::time::sleep(Duration::from_millis(500)).await;
    client
        .dispatch_cancels(vec![(SymbolId(0), "working-b".into())])
        .unwrap();

    served(&mut completions, 1).await;
    assert_eq!(seen(&marks), vec!["cancel working-b".to_string()]);
    assert_eq!(started.elapsed(), Duration::from_millis(500));
    served(&mut completions, 1).await;
    assert_eq!(
        started.elapsed(),
        Duration::from_secs(1),
        "the stop's lane frees first"
    );
    served(&mut completions, 1).await;
    assert_eq!(
        seen(&marks),
        vec![
            "cancel working-b".to_string(),
            "stop 0".to_string(),
            "send opening-a".to_string()
        ]
    );
    assert_eq!(started.elapsed(), HELD_BY_QUOTA);
    let queries = queries.lock().unwrap().queries;
    assert!(
        (1..=16).contains(&queries),
        "the worker asked the venue {queries} times for three commands, so it is spinning"
    );
}

#[tokio::test(start_paused = true)]
async fn a_held_opening_whose_dispatch_authority_is_spent_is_refused_unsent() {
    let venue = QuotaVenue::new();
    venue.hold_openings(HELD_BY_QUOTA);
    let marks = marks(&venue);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    // A dispatch TTL of nothing: the authority is spent the moment it queues,
    // so what is under test is that the refusal does not wait out the hold.
    let spent = CommandAuthority {
        epoch: epoch.current(),
        queued_ns: clock::now_ns(),
        expires_at_ns: clock::now_ns(),
    };
    let started = tokio::time::Instant::now();
    client
        .dispatch_orders(vec![request("opening-a", false)], Some(spent))
        .unwrap();
    let completion = tokio::time::timeout(Duration::from_secs(5), completions.recv())
        .await
        .expect("the held opening was never answered")
        .unwrap();

    let MutationCompletion::Orders { replies, .. } = completion else {
        panic!("expected a placement completion");
    };
    assert!(
        matches!(&replies[0], Err(VenueError::BadRequest(reason))
            if reason.starts_with("authority: expired")),
        "{replies:?}"
    );
    assert!(
        seen(&marks).is_empty(),
        "a refused opening reached the venue"
    );
    assert_eq!(
        started.elapsed(),
        Duration::ZERO,
        "the reservation was held out for a quota window it could never spend"
    );
}

#[tokio::test(start_paused = true)]
async fn an_authority_that_lapses_during_the_hold_is_refused_when_the_hold_ends() {
    let venue = QuotaVenue::new();
    venue.hold_openings(HELD_BY_QUOTA);
    let marks = marks(&venue);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    let started = tokio::time::Instant::now();
    client
        .dispatch_orders(vec![request("opening-a", false)], Some(live(&epoch)))
        .unwrap();
    tokio::task::yield_now().await;
    tokio::time::sleep(Duration::from_millis(500)).await;
    epoch.advance();

    let completion = tokio::time::timeout(Duration::from_secs(30), completions.recv())
        .await
        .expect("the held opening was never answered")
        .unwrap();
    let MutationCompletion::Orders { replies, .. } = completion else {
        panic!("expected a placement completion");
    };
    assert!(
        matches!(&replies[0], Err(VenueError::BadRequest(reason))
            if reason.starts_with("authority: epoch")),
        "{replies:?}"
    );
    assert!(
        seen(&marks).is_empty(),
        "a refused opening reached the venue"
    );
    assert_eq!(started.elapsed(), HELD_BY_QUOTA);
}

#[tokio::test(start_paused = true)]
async fn a_stop_write_held_on_its_own_lane_does_not_hold_an_eligible_opening_or_cancel() {
    let venue = QuotaVenue::new();
    venue.hold_stops(HELD_BY_QUOTA);
    let marks = marks(&venue);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    client.dispatch_stop(SymbolId(0), 100.0, None).unwrap();
    tokio::task::yield_now().await;
    client
        .dispatch_orders(vec![request("opening-a", false)], Some(live(&epoch)))
        .unwrap();
    client
        .dispatch_cancels(vec![(SymbolId(0), "working-b".into())])
        .unwrap();
    served(&mut completions, 3).await;

    let tape = seen(&marks);
    let stop = tape
        .iter()
        .position(|mark| mark == "stop 0")
        .unwrap_or_else(|| panic!("the stop never reached the venue; tape was {tape:?}"));
    assert_eq!(
        stop, 2,
        "the stop's own lane held the other commands: {tape:?}"
    );
    assert_eq!(
        tape[..2].iter().collect::<std::collections::HashSet<_>>(),
        ["cancel working-b".to_string(), "send opening-a".to_string()]
            .iter()
            .collect()
    );
}

// ----------------------------------------------------------- amend batching

fn reprice() -> AmendSpec {
    AmendSpec {
        px: Some(30_000.0),
        qty: None,
        exact_terms: None,
    }
}

fn calls(venue: &QuotaVenue) -> Arc<Mutex<Vec<Vec<String>>>> {
    venue.amend_calls.clone()
}

fn amend_groups(calls: &Arc<Mutex<Vec<Vec<String>>>>) -> Vec<Vec<String>> {
    calls.lock().unwrap().clone()
}

/// Every amend answer the worker produced, by the command id it answers.
async fn amend_replies(
    completions: &mut mpsc::Receiver<MutationCompletion>,
    count: usize,
) -> Vec<(u64, Result<(), VenueError>)> {
    let mut out = Vec::with_capacity(count);
    for _ in 0..count {
        let completion = tokio::time::timeout(Duration::from_secs(30), completions.recv())
            .await
            .expect("a queued amend was never answered")
            .unwrap();
        let MutationCompletion::Amend {
            command_id, reply, ..
        } = completion
        else {
            panic!("expected an amend completion, got {completion:?}");
        };
        out.push((command_id, reply));
    }
    out
}

#[tokio::test(start_paused = true)]
async fn an_amend_group_is_no_larger_than_the_quota_would_take_now() {
    let venue = QuotaVenue::new();
    venue.hold_amends(1, 1, HELD_BY_QUOTA);
    let calls = calls(&venue);
    let quota = venue.quota.clone();
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    client
        .dispatch_amend(SymbolId(0), "amend-a".into(), reprice(), Some(live(&epoch)))
        .unwrap();
    client
        .dispatch_amend(SymbolId(0), "amend-b".into(), reprice(), Some(live(&epoch)))
        .unwrap();
    served(&mut completions, 2).await;

    assert_eq!(
        amend_groups(&calls),
        vec![vec!["amend-a".to_string()], vec!["amend-b".to_string()]],
        "the lane had one free slot, so the first call carries one request and \
         the second waits for the refill"
    );
    assert_eq!(
        quota.lock().unwrap().held_in_call,
        Duration::ZERO,
        "the call waited the amend window out, so the worker was parked inside it"
    );
}

#[tokio::test(start_paused = true)]
async fn a_cancel_arriving_during_an_amend_quota_hold_is_served_before_the_held_amend() {
    let venue = QuotaVenue::new();
    venue.hold_amends(1, 1, HELD_BY_QUOTA);
    let marks = marks(&venue);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    client
        .dispatch_amend(SymbolId(0), "amend-a".into(), reprice(), Some(live(&epoch)))
        .unwrap();
    client
        .dispatch_amend(SymbolId(0), "amend-b".into(), reprice(), Some(live(&epoch)))
        .unwrap();
    tokio::task::yield_now().await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    client
        .dispatch_cancels(vec![(SymbolId(0), "working-c".into())])
        .unwrap();
    served(&mut completions, 3).await;

    let tape = seen(&marks);
    let cancel = tape
        .iter()
        .position(|mark| mark == "cancel working-c")
        .unwrap_or_else(|| panic!("the cancel never reached the venue; tape was {tape:?}"));
    let held = tape
        .iter()
        .position(|mark| mark == "amend amend-b")
        .unwrap_or_else(|| panic!("the held amend never reached the venue; tape was {tape:?}"));
    assert!(
        cancel < held,
        "the cancel waited inside a gateway call serving the amend lane's quota; tape was {tape:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn an_amend_that_expires_during_an_amend_quota_hold_is_refused_unsent() {
    let venue = QuotaVenue::new();
    venue.hold_amends(0, 10, HELD_BY_QUOTA);
    let marks = marks(&venue);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    let clock_guard =
        engine_types::clock::install_virtual(engine_types::clock::wall_ns(), clock::now_ns())
            .unwrap();
    let queued_ns = clock::now_ns();
    let expiring = CommandAuthority {
        epoch: epoch.current(),
        queued_ns,
        expires_at_ns: queued_ns + 1_000_000_000,
    };
    let expiring_id = client
        .dispatch_amend(
            SymbolId(0),
            "amend-expiring".into(),
            reprice(),
            Some(expiring),
        )
        .unwrap();
    let live_id = client
        .dispatch_amend(
            SymbolId(0),
            "amend-live".into(),
            reprice(),
            Some(live(&epoch)),
        )
        .unwrap();
    tokio::task::yield_now().await;
    engine_types::clock::advance_virtual_to(expiring.expires_at_ns + 1).unwrap();

    let replies = amend_replies(&mut completions, 2).await;
    let refused = replies
        .iter()
        .find(|(id, _)| *id == expiring_id)
        .map(|(_, reply)| reply)
        .expect("the expired amend was never answered");
    assert!(
        matches!(refused, Err(VenueError::BadRequest(reason)) if reason.starts_with("authority:")),
        "{refused:?}"
    );
    assert!(
        replies
            .iter()
            .any(|(id, reply)| *id == live_id && reply.is_ok()),
        "the live amend was not sent"
    );
    assert_eq!(
        seen(&marks),
        vec!["amend amend-live".to_string()],
        "a refused amend reached the venue"
    );
    drop(clock_guard);
}

#[tokio::test(start_paused = true)]
async fn one_spent_authority_in_an_amend_group_refuses_only_its_own_command() {
    let venue = QuotaVenue::new();
    let marks = marks(&venue);
    let calls = calls(&venue);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    // A dispatch TTL of nothing: spent the moment it queues.
    let spent = CommandAuthority {
        epoch: epoch.current(),
        queued_ns: clock::now_ns(),
        expires_at_ns: clock::now_ns(),
    };
    let spent_id = client
        .dispatch_amend(SymbolId(0), "amend-spent".into(), reprice(), Some(spent))
        .unwrap();
    let live_id = client
        .dispatch_amend(
            SymbolId(0),
            "amend-live".into(),
            reprice(),
            Some(live(&epoch)),
        )
        .unwrap();
    let replies = amend_replies(&mut completions, 2).await;

    assert!(
        matches!(
            replies.iter().find(|(id, _)| *id == spent_id),
            Some((_, Err(VenueError::BadRequest(reason)))) if reason.starts_with("authority:")
        ),
        "{replies:?}"
    );
    assert!(
        matches!(
            replies.iter().find(|(id, _)| *id == live_id),
            Some((_, Ok(())))
        ),
        "{replies:?}"
    );
    assert_eq!(
        amend_groups(&calls),
        vec![vec!["amend-live".to_string()]],
        "a refused member spent a place in the wire group"
    );
    assert_eq!(seen(&marks), vec!["amend amend-live".to_string()]);
}

#[tokio::test(start_paused = true)]
async fn an_amend_dispatched_without_an_authority_survives_an_epoch_advance() {
    let venue = QuotaVenue::new();
    let marks = marks(&venue);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    client
        .dispatch_amend(SymbolId(0), "exit-reprice".into(), reprice(), None)
        .unwrap();
    epoch.advance();
    epoch.advance();
    served(&mut completions, 1).await;

    assert_eq!(
        seen(&marks),
        vec!["amend exit-reprice".to_string()],
        "a reprice nothing may refuse was refused at the send boundary"
    );
}

#[tokio::test(start_paused = true)]
async fn every_amend_parked_behind_a_quota_hold_is_answered_after_the_client_is_dropped() {
    let venue = QuotaVenue::new();
    venue.hold_amends(0, 1, HELD_BY_QUOTA);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    let mut queued = Vec::new();
    for id in ["amend-a", "amend-b", "amend-c"] {
        queued.push(
            client
                .dispatch_amend(SymbolId(0), id.into(), reprice(), Some(live(&epoch)))
                .unwrap(),
        );
    }
    tokio::task::yield_now().await;
    drop(client);

    let mut answered: Vec<u64> = amend_replies(&mut completions, 3)
        .await
        .into_iter()
        .map(|(id, _)| id)
        .collect();
    answered.sort_unstable();
    queued.sort_unstable();
    assert_eq!(answered, queued, "a parked amend was dropped, not answered");
}

// ------------------------------------------------- lanes and their capacity

/// A venue whose placements wait for the test to let them through, one permit
/// at a time; everything else is answered at once. The worker parks inside
/// the gateway call, which is where a producer flood finds it.
struct ParkedVenue {
    gate: Arc<tokio::sync::Semaphore>,
    marks: Arc<Mutex<Vec<String>>>,
}

impl ParkedVenue {
    fn parked() -> Self {
        ParkedVenue {
            gate: Arc::new(tokio::sync::Semaphore::new(0)),
            marks: Arc::new(Mutex::new(Vec::new())),
        }
    }

    fn open() -> Self {
        let venue = ParkedVenue::parked();
        venue.gate.add_permits(tokio::sync::Semaphore::MAX_PERMITS);
        venue
    }

    fn mark(&self, what: String) {
        self.marks.lock().unwrap().push(what);
    }
}

#[engine_types::async_trait]
impl VenueGateway for ParkedVenue {
    fn caps(&self) -> VenueCaps {
        VenueCaps {
            native_position_stop: true,
            amend_in_place: true,
            set_leverage: false,
            close_position_below_minimum: false,
        }
    }

    async fn account_identity(&mut self) -> Result<AccountIdentity, VenueError> {
        Ok(AccountIdentity {
            venue: "parked".into(),
            user_id: "7000002".into(),
            realm: "demo".into(),
        })
    }

    async fn send_order(&mut self, req: &OrderRequest) -> Result<OrderAck, VenueError> {
        // Marked on arrival, not on release: what the tape records is the
        // worker reaching the gateway, which is where the flood finds it.
        self.mark(format!("send {}", req.client_order_id));
        self.gate
            .acquire()
            .await
            .expect("the gate outlives the venue")
            .forget();
        Ok(OrderAck {
            client_order_id: req.client_order_id.clone(),
            venue_order_id: format!("venue-{}", req.client_order_id),
            sent_ns: clock::now_ns(),
            ack_ns: clock::now_ns(),
        })
    }

    async fn cancel_order(&mut self, _symbol: SymbolId, id: &str) -> Result<(), VenueError> {
        self.mark(format!("cancel {id}"));
        Ok(())
    }

    async fn amend_order(
        &mut self,
        _symbol: SymbolId,
        id: &str,
        _spec: AmendSpec,
    ) -> Result<(), VenueError> {
        self.mark(format!("amend {id}"));
        Ok(())
    }

    async fn set_stop(&mut self, symbol: SymbolId, _trigger_px: f64) -> Result<(), VenueError> {
        self.mark(format!("stop {}", symbol.0));
        Ok(())
    }

    async fn account_view(&mut self) -> Result<AccountView, VenueError> {
        Ok(AccountView {
            exact_amounts: None,
            equity_usdt: 10_000.0,
            available_usdt: 10_000.0,
            positions: Vec::new(),
            observed_ns: clock::now_ns(),
        })
    }

    async fn instrument_rules(&mut self) -> Result<Vec<(Symbol, InstrumentRule)>, VenueError> {
        Ok(Vec::new())
    }

    async fn working_orders(&mut self) -> Result<Vec<VenueOrder>, VenueError> {
        Ok(Vec::new())
    }
}

/// Openings until the client refuses one, counted from `from`.
fn flood(client: &mut VenueClient, epoch: &AuthorityEpoch, from: usize) -> (usize, VenueError) {
    let mut accepted = 0;
    loop {
        let id = format!("flood-{}", from + accepted);
        match client.dispatch_orders(vec![request(&id, false)], Some(live(epoch))) {
            Ok(_) => accepted += 1,
            Err(error) => return (accepted, error),
        }
        assert!(
            accepted <= COMMAND_CAPACITY + READY_ORDINARY_CAPACITY,
            "the ordinary lane took {accepted} openings without refusing one"
        );
    }
}

#[tokio::test(start_paused = true)]
async fn the_ordinary_lane_refuses_a_producer_flood_and_risk_off_still_overtakes_it() {
    let venue = ParkedVenue::parked();
    let gate = venue.gate.clone();
    let marks = venue.marks.clone();
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    client
        .dispatch_orders(vec![request("in-flight", false)], Some(live(&epoch)))
        .unwrap();
    tokio::task::yield_now().await;
    assert_eq!(seen(&marks), vec!["send in-flight".to_string()]);

    // The mailbox fills behind the send the worker is parked in.
    let (mailbox, _) = flood(&mut client, &epoch, 0);
    assert_eq!(mailbox, COMMAND_CAPACITY);

    // One release, and the worker takes a whole ready set out of the mailbox
    // before parking in the next send.
    gate.add_permits(1);
    drain(&mut completions, 1).await;
    for _ in 0..4 {
        tokio::task::yield_now().await;
    }
    assert_eq!(
        seen(&marks).len(),
        2,
        "the worker did not drain the mailbox"
    );
    let (refilled, refusal) = flood(&mut client, &epoch, mailbox);

    let accepted = 1 + mailbox + refilled;
    assert_eq!(
        accepted,
        COMMAND_CAPACITY + READY_ORDINARY_CAPACITY + 1,
        "the ordinary lane and the ready set do not add up to the documented bound"
    );
    let detail = lane_full(&refusal).unwrap_or_else(|| panic!("{refusal:?}"));
    assert!(
        detail.contains("ordinary") && detail.contains(&COMMAND_CAPACITY.to_string()),
        "the refusal names neither the lane nor its capacity: {detail}"
    );

    client
        .dispatch_cancels(vec![(SymbolId(0), "risk-off".into())])
        .unwrap();
    gate.add_permits(1);
    drain(&mut completions, 2).await;
    let tape = seen(&marks);
    let cancel = tape
        .iter()
        .position(|mark| mark == "cancel risk-off")
        .unwrap_or_else(|| panic!("the cancel never reached the venue; tape was {tape:?}"));
    assert_eq!(
        cancel, 2,
        "the cancel waited behind openings the worker was already holding; tape was {tape:?}"
    );
}

#[tokio::test(start_paused = true)]
async fn the_urgent_lane_refuses_a_cancel_burst_and_takes_cancels_again_once_it_drains() {
    let venue = ParkedVenue::parked();
    let gate = venue.gate.clone();
    let marks = venue.marks.clone();
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    client
        .dispatch_orders(vec![request("parking-opening", false)], Some(live(&epoch)))
        .unwrap();
    tokio::task::yield_now().await;
    assert_eq!(seen(&marks), vec!["send parking-opening".to_string()]);

    let mut accepted = 0;
    let refusal = loop {
        match client.dispatch_cancels(vec![(SymbolId(0), format!("pull-{accepted}"))]) {
            Ok(_) => accepted += 1,
            Err(error) => break error,
        }
        assert!(
            accepted <= URGENT_CAPACITY,
            "the urgent lane took {accepted} cancels without refusing one"
        );
    };
    assert_eq!(accepted, URGENT_CAPACITY);
    let detail = lane_full(&refusal).unwrap_or_else(|| panic!("{refusal:?}"));
    assert!(
        detail.contains("urgent") && detail.contains(&URGENT_CAPACITY.to_string()),
        "the refusal names neither the lane nor its capacity: {detail}"
    );

    gate.add_permits(1);
    drain(&mut completions, URGENT_CAPACITY + 1).await;
    client
        .dispatch_cancels(vec![(SymbolId(0), "after-the-drain".into())])
        .expect("the drained urgent lane still refuses cancels");
}

#[tokio::test(start_paused = true)]
async fn an_engine_that_never_reads_completions_plateaus_the_ready_set_and_climbs_the_refusals() {
    let venue = ParkedVenue::open();
    let marks = venue.marks.clone();
    let epoch = AuthorityEpoch::new();
    let (mut client, _completions) = VenueClient::spawn(venue, epoch.clone());
    let gauges = client.gauges();
    let limit = COMMAND_CAPACITY + READY_ORDINARY_CAPACITY + COMPLETION_CAPACITY + 512;

    let mut accepted = 0usize;
    let mut refused = 0usize;
    for n in 0..limit {
        match client.dispatch_orders(
            vec![request(&format!("flood-{n}"), false)],
            Some(live(&epoch)),
        ) {
            Ok(_) => accepted += 1,
            Err(error) => {
                assert!(lane_full(&error).is_some(), "{error:?}");
                refused += 1;
            }
        }
        if n % 64 == 0 {
            tokio::task::yield_now().await;
        }
    }

    assert!(
        refused > 0,
        "the ordinary lane took {accepted} openings with nothing reading completions"
    );
    assert!(
        accepted <= COMMAND_CAPACITY + READY_ORDINARY_CAPACITY + COMPLETION_CAPACITY + 1,
        "the task holds {accepted} commands, more than its stages can name"
    );
    let snapshot = gauges.snapshot(clock::now_ns());
    assert!(
        snapshot.ready_ordinary <= READY_ORDINARY_CAPACITY,
        "the ready set grew to {} with the completion channel full",
        snapshot.ready_ordinary
    );
    assert_eq!(snapshot.refused_ordinary as usize, refused);
    assert_eq!(snapshot.refused_urgent, 0);
    assert_eq!(snapshot.ordinary_capacity, COMMAND_CAPACITY);
    assert_eq!(snapshot.urgent_capacity, URGENT_CAPACITY);
    assert!(
        seen(&marks).len() >= COMPLETION_CAPACITY,
        "the venue stopped answering before the completion channel filled"
    );
}

#[test]
fn one_command_carries_no_more_requests_than_every_producer_already_sends() {
    let epoch = AuthorityEpoch::new();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    let _guard = runtime.enter();
    let (mut client, _completions) = VenueClient::spawn(ParkedVenue::open(), epoch.clone());

    let requests: Vec<_> = (0..=MAX_REQUESTS_PER_COMMAND)
        .map(|n| request(&format!("oversized-{n}"), false))
        .collect();
    assert!(matches!(
        client.dispatch_orders(requests, Some(live(&epoch))),
        Err(VenueError::BadRequest(_))
    ));
    let cancels: Vec<_> = (0..=MAX_REQUESTS_PER_COMMAND)
        .map(|n| (SymbolId(0), format!("oversized-{n}")))
        .collect();
    assert!(matches!(
        client.dispatch_cancels(cancels),
        Err(VenueError::BadRequest(_))
    ));
    assert!(client
        .dispatch_orders(
            (0..MAX_REQUESTS_PER_COMMAND)
                .map(|n| request(&format!("legal-{n}"), false))
                .collect(),
            Some(live(&epoch))
        )
        .is_ok());
}

// ------------------------------------------------------ the dependency index

/// Ready sets drawn from a small alphabet, so cancels and amends name
/// placements that are sometimes queued here and sometimes not.
fn random_ready(next: &mut impl FnMut() -> u64) -> Vec<Queued> {
    let len = (next() % 9) as usize;
    (0..len)
        .map(|arrival| {
            let arrival = arrival as u64;
            let drawn: Vec<String> = (0..4).map(|_| format!("id-{}", next() % 4)).collect();
            let mut drawn = drawn.into_iter();
            let mut id = move || drawn.next().expect("four ids are drawn per command");
            let command = match next() % 7 {
                0 => Command::SendOrders {
                    command_id: arrival,
                    requests: vec![request(&id(), false)],
                    class: DispatchClass::Opening,
                    authority: None,
                },
                1 => Command::SendOrders {
                    command_id: arrival,
                    requests: vec![request(&id(), false), request(&id(), true)],
                    class: DispatchClass::Opening,
                    authority: None,
                },
                2 => Command::SendOrdersWait {
                    requests: vec![request(&id(), true)],
                    class: DispatchClass::RiskReducing,
                    reply: tokio::sync::oneshot::channel().0,
                },
                3 => Command::CancelOrders {
                    command_id: arrival,
                    requests: vec![(SymbolId(0), id()), (SymbolId(0), id())],
                },
                4 => Command::CancelOrdersWait {
                    requests: vec![(SymbolId(0), id())],
                    reply: tokio::sync::oneshot::channel().0,
                },
                5 => Command::Amend {
                    command_id: arrival,
                    symbol: SymbolId(0),
                    client_order_id: id(),
                    spec: reprice(),
                    authority: None,
                },
                _ => Command::AmendWait {
                    symbol: SymbolId(0),
                    client_order_id: id(),
                    spec: reprice(),
                    reply: tokio::sync::oneshot::channel().0,
                },
            };
            Queued {
                arrival,
                queued_ns: arrival,
                command,
            }
        })
        .collect()
}

#[test]
fn the_dependency_index_selects_what_scanning_every_queued_send_selects() {
    let mut state = 0x2545_f491_4f6c_dd1du64;
    let mut next = move || {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        state
    };
    for _ in 0..2_000 {
        let ready = random_ready(&mut next);
        let sends = queued_send_ids(&ready);
        assert_eq!(
            selectable(&ready, &sends),
            selectable_by_scan(&ready),
            "the index and the scan disagree on a ready set of {} commands",
            ready.len()
        );
    }
}

// ----------------------------------------------------------------- shutdown

#[tokio::test(start_paused = true)]
async fn every_lane_and_the_hold_are_answered_after_the_client_is_dropped() {
    let venue = QuotaVenue::new();
    venue.hold_openings(HELD_BY_QUOTA);
    let epoch = AuthorityEpoch::new();
    let (mut client, mut completions) = VenueClient::spawn(venue, epoch.clone());
    let gauges = client.gauges();
    let mut dispatched = 0usize;
    client
        .dispatch_orders(vec![request("held-opening", false)], Some(live(&epoch)))
        .unwrap();
    dispatched += 1;
    tokio::task::yield_now().await;
    // Nothing is awaited from here to the drop, so both mailboxes still hold
    // what they were given when the senders close.
    for n in 0..3 {
        client
            .dispatch_orders(
                vec![request(&format!("queued-opening-{n}"), false)],
                Some(live(&epoch)),
            )
            .unwrap();
        dispatched += 1;
    }
    for n in 0..2 {
        client
            .dispatch_cancels(vec![(SymbolId(0), format!("queued-cancel-{n}"))])
            .unwrap();
        dispatched += 1;
    }
    drop(client);

    let mut answered = 0usize;
    while answered < dispatched {
        tokio::time::timeout(Duration::from_secs(30), completions.recv())
            .await
            .expect("a command held at shutdown was never answered")
            .expect("the worker stopped with commands outstanding");
        answered += 1;
    }
    let snapshot = gauges.snapshot(clock::now_ns());
    let refused = (snapshot.refused_ordinary + snapshot.refused_urgent) as usize;
    assert_eq!(
        answered + refused,
        dispatched,
        "the worker dropped commands on the way out"
    );
    assert!(
        completions.recv().await.is_none(),
        "the worker answered more than it was given"
    );
}
