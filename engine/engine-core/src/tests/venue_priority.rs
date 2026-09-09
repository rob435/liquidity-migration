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
