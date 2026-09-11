//! What the venue task sends first, and what it refuses to send at all.
//!
//! One mutation is in flight at a time. These drive [`VenueClient`] directly,
//! so what is asserted is the worker's own choice among the commands already
//! waiting — not the engine's admission.

use super::*;

use crate::venue_runtime::{send_class, DispatchClass, MutationCompletion, VenueClient};
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
    queries: usize,
    /// What the gateway call itself waited out. Zero means the worker was
    /// free for the hold rather than parked inside the call.
    held_in_call: Duration,
}

struct QuotaVenue {
    marks: Arc<Mutex<Vec<String>>>,
    quota: Arc<Mutex<Quota>>,
}

impl QuotaVenue {
    fn new() -> Self {
        QuotaVenue {
            marks: Arc::new(Mutex::new(Vec::new())),
            quota: Arc::new(Mutex::new(Quota::default())),
        }
    }

    fn hold_openings(&self, hold: Duration) {
        self.quota.lock().unwrap().openings_free_at = Some(tokio::time::Instant::now() + hold);
    }

    fn hold_stops(&self, hold: Duration) {
        self.quota.lock().unwrap().stops_free_at = Some(tokio::time::Instant::now() + hold);
    }

    fn remaining(&self, command: engine_types::QueuedCommand) -> Duration {
        let quota = self.quota.lock().unwrap();
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
        _symbol: SymbolId,
        id: &str,
        _spec: AmendSpec,
    ) -> Result<(), VenueError> {
        self.admit(engine_types::QueuedCommand::Amend { requests: 1 })
            .await;
        self.mark(format!("amend {id}"));
        Ok(())
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
