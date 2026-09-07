//! Variational public market data, by polling.
//!
//! This venue publishes one read-only endpoint and no websocket, so there is
//! nothing to subscribe to: the feed asks for the whole statistics document on
//! a timer and turns each listing into a quote and a ticker. That is slower and
//! coarser than a stream, and it is what the venue offers.
//!
//! Two consequences worth knowing before pricing anything off it:
//!
//! - **The quote is indicative, not a book.** The venue publishes a two-way
//!   price at a size, not resting depth, so [`Quote::bid_qty`] and `ask_qty`
//!   are zero — "not stated", rather than a depth that was made up.
//! - **The prices are as old as the poll.** Every event carries the venue's own
//!   `updated_at` where there is one, so the engine's staleness bound judges
//!   the venue's clock and not ours.
//!
//! The poll lives in its own task, like every other feed here: `next_event` is
//! a channel receive, which loses nothing when the engine's `select!` drops it.

use crate::symbols::intern;
use std::collections::HashMap;
use std::sync::{Arc, RwLock};
use std::time::Duration;

use engine_public::venues::variational::parse::parse_stats;
use engine_public::venues::variational::public::StatsClient;
use engine_public::VariationalRealm;
use engine_types::{Feed, FeedError, MarketEvent, MarketFeed, Subscription, SymbolId};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tracing::warn;

/// How often the statistics document is fetched. The venue rate-limits to ten
/// requests per ten seconds from one address, so this leaves room for the
/// gateway's own reads beside it.
pub const DEFAULT_POLL: Duration = Duration::from_secs(2);

const QUEUE_DEPTH: usize = 256;

pub struct VariationalPublicFeed {
    realm: VariationalRealm,
    base_url: Option<String>,
    poll: Duration,
    /// Name to id, shared with the polling task. One document carries every
    /// listing, so admitting a symbol is only about having an id to deliver
    /// it under — and the task has to see that id the moment it exists.
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    inbox: Option<Inbox>,
    wanted: Arc<RwLock<Vec<Subscription>>>,
}

struct Inbox {
    events: mpsc::Receiver<Result<MarketEvent, FeedError>>,
    worker: JoinHandle<()>,
}

impl VariationalPublicFeed {
    pub fn new(realm: VariationalRealm, subs: &[Subscription]) -> Self {
        Self::build(realm, None, DEFAULT_POLL, subs)
    }

    /// Point the feed at a local server. Tests only.
    pub fn for_test(base_url: &str, poll: Duration, subs: &[Subscription]) -> Self {
        Self::build(
            VariationalRealm::Mainnet,
            Some(base_url.to_string()),
            poll,
            subs,
        )
    }

    fn build(
        realm: VariationalRealm,
        base_url: Option<String>,
        poll: Duration,
        subs: &[Subscription],
    ) -> Self {
        let ids = Arc::new(RwLock::new(HashMap::new()));
        for sub in subs {
            intern(&ids, &sub.symbol.to_uppercase());
        }
        VariationalPublicFeed {
            realm,
            base_url,
            poll,
            ids,
            inbox: None,
            wanted: Arc::new(RwLock::new(
                subs.iter()
                    .cloned()
                    .map(|mut row| {
                        row.symbol = row.symbol.to_uppercase();
                        row
                    })
                    .collect(),
            )),
        }
    }

    /// The id this feed hands out for a symbol, if it follows it.
    pub fn id_of(&self, symbol: &str) -> Option<SymbolId> {
        crate::symbols::resolve(&self.ids, &symbol.to_uppercase())
    }

    /// Start following a symbol this feed was not built with.
    ///
    /// Nothing is sent to the venue: one document carries every listing, so a
    /// new symbol only needs an id to be delivered under.
    pub fn admit(&mut self, symbol: &str, feed: Feed) -> SymbolId {
        let symbol = symbol.to_uppercase();
        let row = Subscription {
            symbol: symbol.clone(),
            feed,
        };
        let mut wanted = self.wanted.write().expect("subscription lock is poisoned");
        if !wanted.contains(&row) {
            wanted.push(row);
        }
        intern(&self.ids, &symbol)
    }

    fn start(&mut self) {
        let (events, inbox) = mpsc::channel(QUEUE_DEPTH);
        let gateway = match &self.base_url {
            Some(url) => StatsClient::for_test(url),
            None => StatsClient::new(self.realm),
        };
        let ids = self.ids.clone();
        let wanted = self.wanted.clone();
        let poll = self.poll;
        let handle = tokio::spawn(async move {
            poll_forever(gateway, ids, wanted, poll, events).await;
        });
        self.inbox = Some(Inbox {
            events: inbox,
            worker: handle,
        });
    }
}

impl Drop for VariationalPublicFeed {
    fn drop(&mut self) {
        if let Some(inbox) = &self.inbox {
            inbox.worker.abort();
        }
    }
}

impl MarketFeed for VariationalPublicFeed {
    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        if self
            .wanted
            .read()
            .expect("subscription lock is poisoned")
            .is_empty()
        {
            return std::future::pending().await;
        }
        if self.inbox.is_none() {
            self.start();
        }
        let inbox = self.inbox.as_mut().expect("started just above");
        match inbox.events.recv().await {
            Some(event) => event,
            None => Err(FeedError::Closed),
        }
    }

    fn retire(&mut self, symbol: &str, feed: Feed) -> bool {
        let symbol = symbol.to_uppercase();
        let mut wanted = self.wanted.write().expect("subscription lock is poisoned");
        let before = wanted.len();
        wanted.retain(|row| row.symbol != symbol || row.feed != feed);
        let changed = wanted.len() != before;
        if changed && wanted.is_empty() {
            if let Some(inbox) = self.inbox.take() {
                inbox.worker.abort();
            }
        }
        changed
    }

    fn admit(&mut self, symbol: &str, feed: Feed) -> Option<SymbolId> {
        Some(VariationalPublicFeed::admit(self, symbol, feed))
    }
}

async fn poll_forever(
    gateway: StatsClient,
    ids: Arc<RwLock<HashMap<String, SymbolId>>>,
    wanted: Arc<RwLock<Vec<Subscription>>>,
    poll: Duration,
    events: mpsc::Sender<Result<MarketEvent, FeedError>>,
) {
    loop {
        match gateway.stats().await {
            Ok(stats) => match parse_stats(&stats) {
                Ok(listings) => {
                    for listing in listings {
                        let (quote_needed, ticker_needed) = {
                            let wanted = wanted.read().expect("subscription lock is poisoned");
                            let symbol = listing.symbol();
                            (
                                wanted
                                    .iter()
                                    .any(|row| row.symbol == symbol && row.feed == Feed::Quote),
                                wanted
                                    .iter()
                                    .any(|row| row.symbol == symbol && row.feed == Feed::Ticker),
                            )
                        };
                        if !quote_needed && !ticker_needed {
                            continue;
                        }
                        let Some(id) = crate::symbols::resolve(&ids, &listing.symbol()) else {
                            continue;
                        };
                        let recv_ns = engine_types::clock::mono_ns();
                        let venue_ts_ms = engine_types::clock::wall_ms();
                        if let Some(quote) =
                            listing.quote(venue_ts_ms, recv_ns).filter(|_| quote_needed)
                        {
                            if events
                                .send(Ok(MarketEvent::Quote { symbol: id, quote }))
                                .await
                                .is_err()
                            {
                                return;
                            }
                        }
                        let ticker = listing.ticker_state(venue_ts_ms, recv_ns);
                        if ticker_needed
                            && events
                                .send(Ok(MarketEvent::Ticker { symbol: id, ticker }))
                                .await
                                .is_err()
                        {
                            return;
                        }
                    }
                }
                Err(e) => {
                    warn!(error = %e, "variational stats were unreadable");
                    if events
                        .send(Err(FeedError::BadMessage(e.to_string())))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
            },
            Err(e) => {
                warn!(error = %e, "variational stats could not be read");
                if events
                    .send(Err(FeedError::Transport(e.to_string())))
                    .await
                    .is_err()
                {
                    return;
                }
            }
        }
        tokio::time::sleep(poll).await;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_symbol_admitted_later_gets_an_id_without_asking_the_venue() {
        // One document carries every listing, so admitting a symbol is only
        // about having somewhere to deliver it.
        let mut feed = VariationalPublicFeed::new(
            VariationalRealm::Mainnet,
            &[Subscription {
                symbol: "BTCUSDT".into(),
                feed: Feed::Quote,
            }],
        );
        assert_eq!(feed.id_of("BTCUSDT"), Some(SymbolId(0)));
        assert_eq!(feed.admit("XAUUSDT", Feed::Ticker), SymbolId(1));
        assert_eq!(feed.admit("XAUUSDT", Feed::Quote), SymbolId(1));
    }

    #[test]
    fn the_poll_leaves_room_under_the_venues_rate_limit() {
        // Ten requests per ten seconds from one address, shared with the
        // gateway's own reads.
        assert!(DEFAULT_POLL >= Duration::from_secs(1), "{DEFAULT_POLL:?}");
    }
}

#[cfg(test)]
#[tokio::test(start_paused = true)]
async fn empty_demand_after_retirement_stays_idle_until_readmission() {
    use std::future::Future;
    let subs = [Subscription {
        symbol: "BTCUSDT".into(),
        feed: Feed::Quote,
    }];
    let mut feed = VariationalPublicFeed::new(VariationalRealm::Mainnet, &subs);
    assert!(MarketFeed::retire(&mut feed, "BTCUSDT", Feed::Quote));
    let mut context = std::task::Context::from_waker(std::task::Waker::noop());
    let mut event = Box::pin(feed.next_event());
    assert!(event.as_mut().poll(&mut context).is_pending());
    drop(event);
    assert!(
        feed.inbox.is_none(),
        "empty demand must not start a socket or poll worker"
    );
    assert_eq!(
        MarketFeed::admit(&mut feed, "BTCUSDT", Feed::Quote),
        Some(SymbolId(0))
    );
    let mut event = Box::pin(feed.next_event());
    assert!(event.as_mut().poll(&mut context).is_pending());
    drop(event);
    assert!(
        feed.inbox.is_some(),
        "readmission restarts the worker with its original symbol ID"
    );
}
