//! Pure one-shot touch decision contract.

use engine_types::{OrderUpdate, Side, TimerId};
use serde::{Deserialize, Serialize};

pub const CHECKPOINT_SCHEMA_VERSION: u16 = 1;
pub const TTL_TIMER: TimerId = TimerId(1);

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct SniperConfig {
    pub symbol: String,
    pub side: Side,
    pub trigger_px: f64,
    pub qty: f64,
    pub stop_px: f64,
    pub take_px: Option<f64>,
    pub ttl_ns: Option<u64>,
}

#[derive(Copy, Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum Phase {
    Armed,
    EntrySent,
    Holding,
    ExitSent,
    Done,
}

#[derive(Clone, Debug, PartialEq)]
pub struct SniperState {
    pub phase: Phase,
    pub entry_side: Side,
    pub entry_order: Option<String>,
    pub exit_order: Option<String>,
    pub open_qty: f64,
    pub exit_working_qty: f64,
    pub exit_rejects: u8,
    pub resend_exit_on_next_quote: bool,
    pub ttl_due_wall_ms: Option<i64>,
}

impl SniperState {
    pub fn armed(side: Side) -> Self {
        Self {
            phase: Phase::Armed,
            entry_side: side,
            entry_order: None,
            exit_order: None,
            open_qty: 0.0,
            exit_working_qty: 0.0,
            exit_rejects: 0,
            resend_exit_on_next_quote: false,
            ttl_due_wall_ms: None,
        }
    }
}

/// Minimal state that must outlive both the process and WAL rotation.
/// Orders and attributed quantity are restored from the engine's own ledger;
/// this proves that the one-shot arm was consumed and retains the wall-clock
/// deadline that a monotonic timer cannot carry across a restart.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct SniperCheckpoint {
    pub consumed: bool,
    pub ttl_due_wall_ms: Option<i64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RestoredOrder {
    pub client_order_id: String,
    pub side: Side,
    pub qty: f64,
    pub filled_qty: f64,
    pub reduce_only: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RestoreInput {
    pub checkpoint: Option<SniperCheckpoint>,
    pub attributed_position: f64,
    pub resting: Vec<RestoredOrder>,
    pub wall_ms: i64,
}

#[derive(Clone, Debug, PartialEq)]
pub enum DecisionInput<'a> {
    Restore(RestoreInput),
    Quote {
        bid_px: f64,
        ask_px: f64,
        wall_ms: i64,
    },
    Timer {
        id: TimerId,
    },
    Order {
        update: &'a OrderUpdate,
        wall_ms: i64,
    },
    IntentRefused {
        reduce_only: bool,
    },
}

#[derive(Clone, Debug, PartialEq)]
pub enum Effect {
    Save(SniperCheckpoint),
    PlaceEntry,
    PlaceExit { side: Side, qty: f64 },
    ArmTtl { after_ns: u64 },
}

#[derive(Clone, Debug, PartialEq)]
pub struct DecisionOutput {
    pub state: SniperState,
    pub effects: Vec<Effect>,
}

pub fn decide(
    input: DecisionInput<'_>,
    prior: &SniperState,
    config: &SniperConfig,
) -> DecisionOutput {
    match input {
        DecisionInput::Restore(input) => restore(input, config),
        DecisionInput::Quote {
            bid_px,
            ask_px,
            wall_ms,
        } => on_quote(prior, config, bid_px, ask_px, wall_ms),
        DecisionInput::Timer { id } => on_timer(prior, id),
        DecisionInput::Order { update, wall_ms } => on_order(prior, config, update, wall_ms),
        DecisionInput::IntentRefused { reduce_only } => refused(prior, reduce_only),
    }
}

fn restore(input: RestoreInput, config: &SniperConfig) -> DecisionOutput {
    let tolerance = config.qty.max(1.0) * 1e-12;
    let opening = input.resting.iter().find(|order| !order.reduce_only);
    let exiting = input.resting.iter().find(|order| order.reduce_only);
    let has_position = input.attributed_position.abs() > tolerance;
    let consumed = input
        .checkpoint
        .is_some_and(|checkpoint| checkpoint.consumed)
        || has_position
        || opening.is_some()
        || exiting.is_some();
    if !consumed {
        return DecisionOutput {
            state: SniperState::armed(config.side),
            effects: Vec::new(),
        };
    }

    let entry_side = if input.attributed_position > tolerance {
        Side::Buy
    } else if input.attributed_position < -tolerance {
        Side::Sell
    } else {
        opening.map_or(config.side, |order| order.side)
    };
    let ttl_due_wall_ms = input
        .checkpoint
        .and_then(|checkpoint| checkpoint.ttl_due_wall_ms);
    let mut state = SniperState {
        phase: Phase::Done,
        entry_side,
        entry_order: opening.map(|order| order.client_order_id.clone()),
        exit_order: exiting.map(|order| order.client_order_id.clone()),
        open_qty: input.attributed_position.abs(),
        exit_working_qty: exiting.map_or(0.0, |order| (order.qty - order.filled_qty).max(0.0)),
        exit_rejects: 0,
        resend_exit_on_next_quote: false,
        ttl_due_wall_ms,
    };
    if exiting.is_some() {
        state.phase = Phase::ExitSent;
    } else if has_position {
        state.phase = Phase::Holding;
    } else if opening.is_some() {
        state.phase = Phase::EntrySent;
    }

    let mut effects = Vec::new();
    if input.checkpoint.is_none() {
        effects.push(Effect::Save(SniperCheckpoint {
            consumed: true,
            ttl_due_wall_ms,
        }));
    }
    if state.phase == Phase::Holding {
        if let Some(ttl_ns) = config.ttl_ns {
            match ttl_due_wall_ms {
                Some(due) if due > input.wall_ms => effects.push(Effect::ArmTtl {
                    after_ns: ((due - input.wall_ms) as u64).saturating_mul(1_000_000),
                }),
                // No recovered fill time is not permission to extend the
                // holding clock. Close now instead of inventing a deadline.
                _ => place_exit(&mut state, &mut effects),
            }
            let _ = ttl_ns;
        }
    }
    DecisionOutput { state, effects }
}

fn on_quote(
    prior: &SniperState,
    config: &SniperConfig,
    bid_px: f64,
    ask_px: f64,
    _wall_ms: i64,
) -> DecisionOutput {
    let mut state = prior.clone();
    let mut effects = Vec::new();
    match state.phase {
        Phase::Armed if entry_touched(config.side, config.trigger_px, bid_px, ask_px) => {
            state.phase = Phase::EntrySent;
            effects.push(Effect::Save(SniperCheckpoint {
                consumed: true,
                ttl_due_wall_ms: None,
            }));
            effects.push(Effect::PlaceEntry);
        }
        Phase::Holding if take_touched(state.entry_side, config.take_px, bid_px, ask_px) => {
            place_exit(&mut state, &mut effects);
        }
        Phase::ExitSent if state.resend_exit_on_next_quote => {
            place_exit(&mut state, &mut effects);
        }
        _ => {}
    }
    DecisionOutput { state, effects }
}

fn on_timer(prior: &SniperState, id: TimerId) -> DecisionOutput {
    let mut state = prior.clone();
    let mut effects = Vec::new();
    if id == TTL_TIMER && state.phase == Phase::Holding {
        place_exit(&mut state, &mut effects);
    }
    DecisionOutput { state, effects }
}

fn on_order(
    prior: &SniperState,
    config: &SniperConfig,
    update: &OrderUpdate,
    wall_ms: i64,
) -> DecisionOutput {
    let mut state = prior.clone();
    let mut effects = Vec::new();
    match state.phase {
        Phase::EntrySent => match update {
            OrderUpdate::Ack(ack) if is_ours(&state.entry_order, &ack.client_order_id) => {
                state.entry_order = Some(ack.client_order_id.clone());
            }
            OrderUpdate::Fill {
                client_order_id,
                qty,
                ..
            } if is_ours(&state.entry_order, client_order_id) => {
                state.entry_order = Some(client_order_id.clone());
                state.open_qty += qty;
                state.phase = Phase::Holding;
                if let Some(ttl_ns) = config.ttl_ns {
                    let due = wall_ms.saturating_add((ttl_ns / 1_000_000) as i64);
                    state.ttl_due_wall_ms = Some(due);
                    effects.push(Effect::Save(SniperCheckpoint {
                        consumed: true,
                        ttl_due_wall_ms: Some(due),
                    }));
                    effects.push(Effect::ArmTtl { after_ns: ttl_ns });
                }
            }
            OrderUpdate::Reject {
                client_order_id, ..
            }
            | OrderUpdate::Cancelled {
                client_order_id, ..
            } if is_ours(&state.entry_order, client_order_id) => state.phase = Phase::Done,
            _ => {}
        },
        Phase::Holding => {
            if let OrderUpdate::Fill {
                client_order_id,
                qty,
                ..
            } = update
            {
                if is_ours(&state.entry_order, client_order_id) {
                    state.open_qty += qty;
                }
            }
        }
        Phase::ExitSent => match update {
            OrderUpdate::Ack(ack)
                if state.entry_order.as_deref() == Some(ack.client_order_id.as_str()) => {}
            OrderUpdate::Ack(ack) if is_ours(&state.exit_order, &ack.client_order_id) => {
                state.exit_order = Some(ack.client_order_id.clone());
            }
            OrderUpdate::Fill {
                client_order_id,
                qty,
                ..
            } if is_ours(&state.entry_order, client_order_id) => state.open_qty += qty,
            OrderUpdate::Fill {
                client_order_id,
                qty,
                ..
            } if is_ours(&state.exit_order, client_order_id) => {
                state.open_qty = (state.open_qty - qty).max(0.0);
                state.exit_working_qty = (state.exit_working_qty - qty).max(0.0);
                let tolerance = config.qty.max(1.0) * 1e-12;
                if state.open_qty <= tolerance {
                    state.phase = Phase::Done;
                } else if state.exit_working_qty <= tolerance {
                    state.resend_exit_on_next_quote = true;
                }
            }
            OrderUpdate::Cancelled {
                client_order_id, ..
            }
            | OrderUpdate::Reject {
                client_order_id, ..
            } if state.entry_order.as_deref() == Some(client_order_id.as_str()) => {}
            OrderUpdate::Cancelled {
                client_order_id, ..
            } if is_ours(&state.exit_order, client_order_id) => {
                if state.open_qty <= config.qty.max(1.0) * 1e-12 {
                    state.phase = Phase::Done;
                } else {
                    state.resend_exit_on_next_quote = true;
                }
            }
            OrderUpdate::Reject {
                client_order_id, ..
            } if is_ours(&state.exit_order, client_order_id) => {
                state.exit_rejects = state.exit_rejects.saturating_add(1);
                if state.exit_rejects >= 2 {
                    state.phase = Phase::Done;
                } else {
                    state.resend_exit_on_next_quote = true;
                }
            }
            _ => {}
        },
        Phase::Done => {
            if let OrderUpdate::Fill {
                client_order_id,
                qty,
                ..
            } = update
            {
                if is_ours(&state.entry_order, client_order_id) {
                    state.open_qty += qty;
                    if state.open_qty > config.qty.max(1.0) * 1e-12 {
                        state.phase = Phase::ExitSent;
                        state.resend_exit_on_next_quote = true;
                    }
                }
            }
        }
        Phase::Armed => {}
    }
    DecisionOutput { state, effects }
}

fn refused(prior: &SniperState, reduce_only: bool) -> DecisionOutput {
    let mut state = prior.clone();
    if !reduce_only && state.phase == Phase::EntrySent {
        state.phase = Phase::Done;
    } else if reduce_only && state.phase == Phase::ExitSent {
        state.exit_rejects = state.exit_rejects.saturating_add(1);
        if state.exit_rejects >= 2 {
            state.phase = Phase::Done;
        } else {
            state.resend_exit_on_next_quote = true;
        }
    }
    DecisionOutput {
        state,
        effects: Vec::new(),
    }
}

fn place_exit(state: &mut SniperState, effects: &mut Vec<Effect>) {
    let qty = state.open_qty.max(0.0);
    if qty <= 1e-12 {
        state.phase = Phase::Done;
        return;
    }
    state.exit_order = None;
    state.exit_working_qty = qty;
    state.resend_exit_on_next_quote = false;
    state.phase = Phase::ExitSent;
    effects.push(Effect::PlaceExit {
        side: state.entry_side.flipped(),
        qty,
    });
}

fn entry_touched(side: Side, trigger_px: f64, bid_px: f64, ask_px: f64) -> bool {
    match side {
        Side::Buy => ask_px > 0.0 && ask_px <= trigger_px,
        Side::Sell => bid_px >= trigger_px,
    }
}

fn take_touched(side: Side, take_px: Option<f64>, bid_px: f64, ask_px: f64) -> bool {
    let Some(take_px) = take_px else {
        return false;
    };
    match side {
        Side::Buy => bid_px >= take_px,
        Side::Sell => ask_px > 0.0 && ask_px <= take_px,
    }
}

fn is_ours(known: &Option<String>, client_order_id: &str) -> bool {
    known.as_deref().is_none_or(|ours| ours == client_order_id)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn config() -> SniperConfig {
        SniperConfig {
            symbol: "BTCUSDT".into(),
            side: Side::Buy,
            trigger_px: 100.0,
            qty: 2.0,
            stop_px: 95.0,
            take_px: Some(110.0),
            ttl_ns: Some(60_000_000_000),
        }
    }

    #[test]
    fn checkpoint_is_decided_before_the_entry() {
        let config = config();
        let out = decide(
            DecisionInput::Quote {
                bid_px: 99.9,
                ask_px: 100.0,
                wall_ms: 1,
            },
            &SniperState::armed(config.side),
            &config,
        );
        assert!(matches!(
            out.effects.as_slice(),
            [Effect::Save(_), Effect::PlaceEntry]
        ));
    }

    #[test]
    fn a_consumed_flat_arm_restores_done() {
        let config = config();
        let out = decide(
            DecisionInput::Restore(RestoreInput {
                checkpoint: Some(SniperCheckpoint {
                    consumed: true,
                    ttl_due_wall_ms: None,
                }),
                attributed_position: 0.0,
                resting: Vec::new(),
                wall_ms: 1,
            }),
            &SniperState::armed(config.side),
            &config,
        );
        assert_eq!(out.state.phase, Phase::Done);
    }

    #[test]
    fn an_attributed_position_restores_holding_not_armed() {
        let mut config = config();
        config.ttl_ns = None;
        let out = decide(
            DecisionInput::Restore(RestoreInput {
                checkpoint: Some(SniperCheckpoint {
                    consumed: true,
                    ttl_due_wall_ms: None,
                }),
                attributed_position: 1.25,
                resting: Vec::new(),
                wall_ms: 1,
            }),
            &SniperState::armed(config.side),
            &config,
        );
        assert_eq!(out.state.phase, Phase::Holding);
        assert_eq!(out.state.open_qty, 1.25);
    }

    #[test]
    fn a_recovered_ttl_uses_only_its_remaining_time() {
        let config = config();
        let out = decide(
            DecisionInput::Restore(RestoreInput {
                checkpoint: Some(SniperCheckpoint {
                    consumed: true,
                    ttl_due_wall_ms: Some(90_000),
                }),
                attributed_position: 2.0,
                resting: Vec::new(),
                wall_ms: 80_000,
            }),
            &SniperState::armed(config.side),
            &config,
        );
        assert_eq!(
            out.effects,
            vec![Effect::ArmTtl {
                after_ns: 10_000_000_000
            }]
        );
    }

    #[test]
    fn a_fill_and_its_still_resting_entry_restore_as_holding() {
        let config = config();
        let out = decide(
            DecisionInput::Restore(RestoreInput {
                checkpoint: Some(SniperCheckpoint {
                    consumed: true,
                    ttl_due_wall_ms: Some(90_000),
                }),
                attributed_position: 0.75,
                resting: vec![RestoredOrder {
                    client_order_id: "part-filled-entry".into(),
                    side: Side::Buy,
                    qty: 2.0,
                    filled_qty: 0.75,
                    reduce_only: false,
                }],
                wall_ms: 80_000,
            }),
            &SniperState::armed(config.side),
            &config,
        );
        assert_eq!(out.state.phase, Phase::Holding);
        assert_eq!(out.state.entry_order.as_deref(), Some("part-filled-entry"));
        assert_eq!(out.state.open_qty, 0.75);
        assert_eq!(
            out.effects,
            vec![Effect::ArmTtl {
                after_ns: 10_000_000_000
            }]
        );
    }
}
