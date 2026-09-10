//! What woke the reducer that decided an order.
//!
//! The log already holds the decision ([`crate::WalRecord::Intent`]) and the
//! input ([`crate::WalRecord::SignalObservation`]); it held nothing that
//! joined them. [`DecisionCause`] rides on the intent record and names the
//! callback the decision came out of, so a source row can be followed to its
//! order and its fill instead of guessed at by adjacency.

use serde::{Deserialize, Serialize};

use crate::ids::{StrategyId, SymbolId, TimerId};
use crate::market::MarketEvent;
use crate::orders::OrderUpdate;
use crate::strategy::EngineEvent;

/// One thing that woke a reducer. Wire kinds are frozen: a reader older than
/// the writer must still parse the causes it knows.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Cause {
    Boot,
    Market {
        symbol: SymbolId,
    },
    FeedReset,
    Timer {
        id: TimerId,
    },
    Order {
        /// Empty when the update carries no order id: a venue-native stop
        /// fill, `stop_attached`, or a private-stream reset.
        client_order_id: String,
    },
    Signal {
        source: String,
        sequence: u64,
        observation_id: String,
    },
    StrategyEvent {
        source: StrategyId,
        event_id: String,
    },
    IntentRefused {
        symbol: SymbolId,
    },
    EntryPermission {
        request_id: String,
    },
    FlattenDirectional {
        request_id: String,
    },
    /// The engine's own working supervisor continuing a resting entry.
    Working {
        client_order_id: String,
    },
    /// An effect replayed from the log at boot; the original cause is on the
    /// earlier record.
    Restored,
}

impl From<&EngineEvent> for Cause {
    fn from(event: &EngineEvent) -> Self {
        match event {
            EngineEvent::Boot => Self::Boot,
            EngineEvent::Market(market) => match market {
                MarketEvent::Quote { symbol, .. }
                | MarketEvent::Depth { symbol, .. }
                | MarketEvent::Trades { symbol, .. }
                | MarketEvent::Ticker { symbol, .. } => Self::Market { symbol: *symbol },
                MarketEvent::FeedReset { .. } => Self::FeedReset,
            },
            EngineEvent::Timer { id, .. } => Self::Timer { id: *id },
            EngineEvent::Order(update) => Self::Order {
                client_order_id: order_id_of(update),
            },
            EngineEvent::Signal(observation) => Self::Signal {
                source: observation.source.clone(),
                sequence: observation.sequence,
                observation_id: observation.observation_id.clone(),
            },
            EngineEvent::StrategyEvent(event) => Self::StrategyEvent {
                source: event.source,
                event_id: event.event_id.clone(),
            },
            EngineEvent::IntentRefused { symbol, .. } => Self::IntentRefused { symbol: *symbol },
            EngineEvent::EntryPermission { request_id, .. } => Self::EntryPermission {
                request_id: request_id.clone(),
            },
            EngineEvent::FlattenDirectional { request_id } => Self::FlattenDirectional {
                request_id: request_id.clone(),
            },
        }
    }
}

/// The order id the update names, or empty when it names none.
fn order_id_of(update: &OrderUpdate) -> String {
    match update {
        OrderUpdate::Ack(ack) => ack.client_order_id.clone(),
        OrderUpdate::Reject {
            client_order_id, ..
        }
        | OrderUpdate::Fill {
            client_order_id, ..
        }
        | OrderUpdate::FastFill {
            client_order_id, ..
        }
        | OrderUpdate::Amended {
            client_order_id, ..
        }
        | OrderUpdate::Cancelled {
            client_order_id, ..
        } => client_order_id.clone(),
        OrderUpdate::StopAttached { .. } | OrderUpdate::StreamReset { .. } => String::new(),
    }
}

/// The callback one decision came out of. Carried on the intent record, not
/// inside [`crate::Intent`], so the strategy-facing struct is unchanged.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct DecisionCause {
    /// Engine realtime ms at callback delivery. `Intent.decided_ns` is engine
    /// monotonic ns, so both clock domains are on the record and neither is
    /// subtracted from the other.
    pub callback_wall_ms: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub callback_id: Option<u64>,
    /// Immediate cause first. Reducer-declared further causes are a deferred
    /// SDK hook.
    pub causes: Vec<Cause>,
}

impl DecisionCause {
    /// The wake this decision came directly out of.
    pub fn immediate(&self) -> Option<&Cause> {
        self.causes.first()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::orders::{OrderAck, Side};

    #[test]
    fn every_engine_event_names_a_cause() {
        let observation = crate::strategy::SignalObservation {
            schema_version: crate::SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: "f".into(),
            destination: StrategyId(1),
            source: "worker.long".into(),
            sequence: 9,
            observation_id: "obs-9".into(),
            kind: "long_feature_batch".into(),
            observed_wall_ts_ms: 1,
            available_wall_ts_ms: 2,
            subscriptions: Vec::new(),
            payload: b"{}".to_vec(),
            content_sha256: String::new(),
        };
        let cases = [
            (EngineEvent::Boot, Cause::Boot),
            (
                EngineEvent::Market(MarketEvent::Quote {
                    symbol: SymbolId(3),
                    quote: Default::default(),
                }),
                Cause::Market {
                    symbol: SymbolId(3),
                },
            ),
            (
                EngineEvent::Market(MarketEvent::FeedReset { recv_ns: 7 }),
                Cause::FeedReset,
            ),
            (
                EngineEvent::Timer {
                    id: TimerId(4),
                    now_ns: 5,
                },
                Cause::Timer { id: TimerId(4) },
            ),
            (
                EngineEvent::Order(OrderUpdate::Cancelled {
                    client_order_id: "eng-1".into(),
                    recv_ns: 1,
                }),
                Cause::Order {
                    client_order_id: "eng-1".into(),
                },
            ),
            (
                EngineEvent::Order(OrderUpdate::StopAttached {
                    symbol: SymbolId(0),
                    trigger_px: 1.0,
                    recv_ns: 1,
                }),
                Cause::Order {
                    client_order_id: String::new(),
                },
            ),
            (
                EngineEvent::Signal(observation),
                Cause::Signal {
                    source: "worker.long".into(),
                    sequence: 9,
                    observation_id: "obs-9".into(),
                },
            ),
            (
                EngineEvent::StrategyEvent(crate::strategy::StrategyEvent {
                    source: StrategyId(2),
                    destination: StrategyId(1),
                    kind: "k".into(),
                    event_id: "ev-1".into(),
                    payload: Vec::new(),
                }),
                Cause::StrategyEvent {
                    source: StrategyId(2),
                    event_id: "ev-1".into(),
                },
            ),
            (
                EngineEvent::IntentRefused {
                    symbol: SymbolId(6),
                    reduce_only: false,
                    reason: "engine_latched".into(),
                },
                Cause::IntentRefused {
                    symbol: SymbolId(6),
                },
            ),
            (
                EngineEvent::EntryPermission {
                    request_id: "req-1".into(),
                    entries_enabled: true,
                },
                Cause::EntryPermission {
                    request_id: "req-1".into(),
                },
            ),
            (
                EngineEvent::FlattenDirectional {
                    request_id: "req-2".into(),
                },
                Cause::FlattenDirectional {
                    request_id: "req-2".into(),
                },
            ),
        ];
        for (event, want) in cases {
            assert_eq!(Cause::from(&event), want, "{event:?}");
        }
    }

    #[test]
    fn a_venue_stop_fill_with_no_id_causes_an_order_wake_with_an_empty_id() {
        let event = EngineEvent::Order(OrderUpdate::Fill {
            allocation: None,
            amounts: None,
            exec_id: "exec-1".into(),
            client_order_id: String::new(),
            symbol: SymbolId(0),
            side: Side::Sell,
            qty: 1.0,
            px: 100.0,
            fee: None,
            is_maker: false,
            forced_close: None,
            venue_ts_ms: 1,
            recv_ns: 1,
        });
        assert_eq!(
            Cause::from(&event),
            Cause::Order {
                client_order_id: String::new()
            }
        );
    }

    #[test]
    fn an_ack_names_the_order_it_answers() {
        let event = EngineEvent::Order(OrderUpdate::Ack(OrderAck {
            client_order_id: "eng-7".into(),
            venue_order_id: "v-7".into(),
            sent_ns: 0,
            ack_ns: 1,
        }));
        assert_eq!(
            Cause::from(&event),
            Cause::Order {
                client_order_id: "eng-7".into()
            }
        );
    }

    #[test]
    fn a_cause_round_trips_through_json() {
        let cause = DecisionCause {
            callback_wall_ms: 1_700_000_000_000,
            callback_id: Some(42),
            causes: vec![Cause::Signal {
                source: "worker.long".into(),
                sequence: 3,
                observation_id: "obs-3".into(),
            }],
        };
        let text = serde_json::to_string(&cause).unwrap();
        assert_eq!(
            text,
            r#"{"callback_wall_ms":1700000000000,"callback_id":42,"causes":[{"kind":"signal","source":"worker.long","sequence":3,"observation_id":"obs-3"}]}"#
        );
        assert_eq!(
            serde_json::from_str::<DecisionCause>(&text).unwrap(),
            cause,
            "the wire shape must read back as itself"
        );
    }

    #[test]
    fn a_cause_without_a_callback_id_omits_the_field_and_reads_back_none() {
        let cause = DecisionCause {
            callback_wall_ms: 5,
            callback_id: None,
            causes: vec![Cause::Restored],
        };
        let text = serde_json::to_string(&cause).unwrap();
        assert_eq!(
            text,
            r#"{"callback_wall_ms":5,"causes":[{"kind":"restored"}]}"#
        );
        assert_eq!(serde_json::from_str::<DecisionCause>(&text).unwrap(), cause);
    }
}
