//! Deciding where a two-sided quote should sit, and what to do about the one
//! already resting.
//!
//! Pure arithmetic, like the target-book planner: the market, the orders this
//! strategy already has out, and the position are arguments, so every rule
//! here can be tested on its own. This is the other kind of strategy the
//! engine carries — one whose whole decision is in the loop, reacting to a
//! price in microseconds rather than following a book decided hours ago.

use engine_types::{BookLevel, Depth, Quote, Side, TradeFlow};
use serde::{Deserialize, Serialize};

/// Where a quote should be and how far it may drift before it is worth
/// moving.
#[derive(Copy, Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct QuoteRules {
    /// Half the quoted spread, as a fraction of mid. A quote sits this far
    /// from mid on each side.
    pub half_spread: f64,
    /// How far a resting quote may sit from where it should be before it is
    /// moved. Every move costs a request and gives up queue position, so a
    /// quote that is nearly right is left alone.
    pub requote_tolerance: f64,
    /// Size per side, in base units.
    pub qty: f64,
    /// Inventory ceiling in base units. A side that would push the position
    /// past this stops being quoted.
    pub max_position: f64,
    /// How far the quote's centre moves away from the inventory when the
    /// position is at its ceiling, as a fraction of mid. Zero quotes
    /// symmetrically around the market whatever is held.
    ///
    /// This is the difference between a maker and a thing that accumulates.
    /// Without it the only answer to inventory is the ceiling, which is a
    /// wall: quote both sides at full size until the position hits the limit,
    /// then stop one side outright and wait for the market to come back. With
    /// it, every fill on one side moves both quotes away from that side, so
    /// the book pays a little more to be taken out of its position and a
    /// little less to be put further into it. It works out of inventory
    /// continuously instead of parking at the wall.
    pub skew: f64,
    /// How far below (bid) or above (ask) the fill a stop sits. The risk
    /// kernel refuses a position-opening order without one, and a quote is
    /// position-opening.
    pub stop_loss_fraction: f64,
}

/// Every setting used by the short-horizon signal and adaptive quote price.
/// The live plug and the research replay both call this contract.
#[derive(Copy, Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct MicroRules {
    pub maker_fee: f64,
    pub min_edge: f64,
    pub volatility_multiplier: f64,
    pub toxicity: f64,
    pub book_lean: f64,
    pub trade_lean: f64,
    pub signal_half_life_ns: u64,
    pub flow_fast_half_life_ns: u64,
    pub flow_slow_half_life_ns: u64,
    pub flow_fast_weight: f64,
    pub flow_slow_weight: f64,
    pub flow_response: f64,
    pub flow_max_widen: f64,
    pub flow_pull_score: Option<f64>,
    pub flow_depth_bps: f64,
    pub flow_volatility_depth_multiplier: f64,
    pub flow_max_score: f64,
    pub queue_reprice_edge: f64,
    pub qty_usdt: Option<f64>,
    pub max_position_usdt: Option<f64>,
    pub adaptive: bool,
}

/// Durable-in-memory signal state. A market event is reduced into a new value;
/// the plug never edits one of these fields itself.
#[derive(Copy, Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct MicroState {
    pub last_ns: u64,
    pub flow_last_ns: u64,
    pub touch_ns: u64,
    pub var_mid: f64,
    pub microprice: f64,
    pub book_imbalance: f64,
    pub variance: f64,
    pub trade_imbalance: f64,
    pub trade_qty: f64,
    pub flow_fast: f64,
    pub flow_slow: f64,
    pub last_depth_ratio: f64,
    pub bid_depth_usdt: f64,
    pub ask_depth_usdt: f64,
    pub has_flow: bool,
    pub has_depth: bool,
}

/// One market observation for the pure signal reducer.
#[derive(Copy, Clone, Debug)]
pub enum SignalInput<'a> {
    Touch {
        bid: BookLevel,
        ask: BookLevel,
        recv_ns: u64,
    },
    Depth {
        bids: &'a [BookLevel],
        asks: &'a [BookLevel],
        recv_ns: u64,
    },
    Trades(TradeFlow),
}

/// Reduce one public market observation. Time is monotonic even when the
/// venue's touch and depth topics arrive out of order.
pub fn reduce_micro(
    mut state: MicroState,
    input: SignalInput<'_>,
    rules: MicroRules,
) -> MicroState {
    match input {
        SignalInput::Touch { bid, ask, recv_ns } => {
            decay_to(&mut state, recv_ns, rules);
            take_touch(&mut state, bid, ask, recv_ns);
        }
        SignalInput::Depth {
            bids,
            asks,
            recv_ns,
        } => {
            let (Some(bid), Some(ask)) = (bids.first().copied(), asks.first().copied()) else {
                return state;
            };
            if bid.px <= 0.0 || ask.px < bid.px {
                return state;
            }
            let decay = decay_to(&mut state, recv_ns, rules);
            take_touch(&mut state, bid, ask, recv_ns);
            let mid = (bid.px + ask.px) / 2.0;
            if state.var_mid > 0.0 {
                let change = (mid / state.var_mid).ln();
                state.variance += (1.0 - decay).max(0.01) * change * change;
            }
            let bid_weight: f64 = bids
                .iter()
                .enumerate()
                .map(|(index, level)| level.qty / (index + 1) as f64)
                .sum();
            let ask_weight: f64 = asks
                .iter()
                .enumerate()
                .map(|(index, level)| level.qty / (index + 1) as f64)
                .sum();
            let total_weight = bid_weight + ask_weight;
            state.book_imbalance = if total_weight > 0.0 {
                (bid_weight - ask_weight) / total_weight
            } else {
                0.0
            };
            state.var_mid = mid;
            let volatility_bps = state.variance.max(0.0).sqrt() * 10_000.0;
            let band_bps = (rules.flow_depth_bps
                + rules.flow_volatility_depth_multiplier * volatility_bps)
                .clamp(rules.flow_depth_bps, 100.0);
            let band = band_bps / 10_000.0;
            state.bid_depth_usdt = bids
                .iter()
                .enumerate()
                .filter(|(index, level)| *index == 0 || level.px >= mid * (1.0 - band))
                .map(|(_, level)| level.px * level.qty)
                .sum();
            state.ask_depth_usdt = asks
                .iter()
                .enumerate()
                .filter(|(index, level)| *index == 0 || level.px <= mid * (1.0 + band))
                .map(|(_, level)| level.px * level.qty)
                .sum();
            state.has_depth = true;
        }
        SignalInput::Trades(trades) => {
            let total = trades.buy_qty + trades.sell_qty;
            if total <= 0.0 {
                return state;
            }
            let decay = decay_to(&mut state, trades.recv_ns, rules);
            let observed = (trades.buy_qty - trades.sell_qty) / total;
            state.trade_imbalance += (1.0 - decay).max(0.05) * (observed - state.trade_imbalance);
            state.trade_qty += total;
            let px = if trades.last_px > 0.0 {
                trades.last_px
            } else {
                state.microprice
            };
            if px > 0.0 && state.bid_depth_usdt > 0.0 && state.ask_depth_usdt > 0.0 {
                let buy_ratio = trades.buy_qty * px / state.ask_depth_usdt;
                let sell_ratio = trades.sell_qty * px / state.bid_depth_usdt;
                let shock =
                    (buy_ratio - sell_ratio).clamp(-rules.flow_max_score, rules.flow_max_score);
                state.last_depth_ratio = shock;
                state.flow_fast =
                    (state.flow_fast + shock).clamp(-rules.flow_max_score, rules.flow_max_score);
                state.flow_slow =
                    (state.flow_slow + shock).clamp(-rules.flow_max_score, rules.flow_max_score);
                state.has_flow = true;
            }
        }
    }
    state
}

fn decay_to(state: &mut MicroState, now_ns: u64, rules: MicroRules) -> f64 {
    let now_ns = now_ns.max(state.last_ns);
    let signal_decay = if state.last_ns == 0 || rules.signal_half_life_ns == 0 {
        state.last_ns = now_ns;
        0.0
    } else {
        let elapsed = now_ns.saturating_sub(state.last_ns) as f64;
        let decay = (-std::f64::consts::LN_2 * elapsed / rules.signal_half_life_ns as f64).exp();
        state.variance *= decay;
        state.trade_imbalance *= decay;
        state.trade_qty *= decay;
        state.last_ns = now_ns;
        decay
    };
    if state.flow_last_ns == 0 {
        state.flow_last_ns = now_ns;
    } else {
        let flow_now = now_ns.max(state.flow_last_ns);
        let elapsed = flow_now.saturating_sub(state.flow_last_ns) as f64;
        if rules.flow_fast_half_life_ns > 0 {
            state.flow_fast *=
                (-std::f64::consts::LN_2 * elapsed / rules.flow_fast_half_life_ns as f64).exp();
        }
        if rules.flow_slow_half_life_ns > 0 {
            state.flow_slow *=
                (-std::f64::consts::LN_2 * elapsed / rules.flow_slow_half_life_ns as f64).exp();
        }
        state.flow_last_ns = flow_now;
    }
    signal_decay
}

fn take_touch(state: &mut MicroState, bid: BookLevel, ask: BookLevel, recv_ns: u64) {
    if bid.px <= 0.0 || ask.px < bid.px || recv_ns < state.touch_ns {
        return;
    }
    state.touch_ns = recv_ns;
    let top_qty = bid.qty + ask.qty;
    state.microprice = if top_qty > 0.0 {
        (ask.px * bid.qty + bid.px * ask.qty) / top_qty
    } else {
        (bid.px + ask.px) / 2.0
    };
}

pub fn flow_score(state: &MicroState, rules: MicroRules) -> f64 {
    let weight = rules.flow_fast_weight + rules.flow_slow_weight;
    if weight <= 0.0 {
        return 0.0;
    }
    ((rules.flow_fast_weight * state.flow_fast + rules.flow_slow_weight * state.flow_slow) / weight)
        .clamp(-rules.flow_max_score, rules.flow_max_score)
}

#[derive(Copy, Clone, Debug, PartialEq)]
pub struct PricedQuote {
    pub fair_px: f64,
    pub rules: QuoteRules,
    pub protection: SideProtection,
}

/// Price the adaptive rule from one immutable state snapshot.
pub fn price_rule(
    quote: Quote,
    depth: &Depth,
    state: Option<&MicroState>,
    resting: &[Resting],
    base: QuoteRules,
    micro: MicroRules,
) -> PricedQuote {
    let mut rules = base;
    let mid = (quote.bid_px + quote.ask_px) / 2.0;
    if mid > 0.0 && mid.is_finite() {
        if let Some(notional) = micro.qty_usdt {
            rules.qty = notional / mid;
        }
        if let Some(notional) = micro.max_position_usdt {
            rules.max_position = notional / mid;
        }
    }
    let Some(state) = state.filter(|state| state.has_depth) else {
        return PricedQuote {
            fair_px: mid,
            rules,
            protection: SideProtection::default(),
        };
    };
    if mid <= 0.0 || !mid.is_finite() || !micro.adaptive {
        return PricedQuote {
            fair_px: mid,
            rules,
            protection: SideProtection::default(),
        };
    }
    let fair_px = state.microprice
        + mid * (micro.book_lean * state.book_imbalance + micro.trade_lean * state.trade_imbalance);
    let cost_floor = micro.maker_fee
        + micro.min_edge
        + micro.volatility_multiplier * state.variance.max(0.0).sqrt()
        + micro.toxicity * state.trade_imbalance.abs();
    rules.half_spread = rules.half_spread.max(cost_floor);
    let score = flow_score(state, micro);
    let extra = (micro.flow_response * score.abs()).min(micro.flow_max_widen);
    let pull = micro.flow_pull_score.and_then(|threshold| {
        if score >= threshold {
            Some(Side::Sell)
        } else if score <= -threshold {
            Some(Side::Buy)
        } else {
            None
        }
    });
    let protection = SideProtection {
        bid_extra: if score < 0.0 { extra } else { 0.0 },
        ask_extra: if score > 0.0 { extra } else { 0.0 },
        pull,
    };
    if micro.queue_reprice_edge > 0.0 && !resting.is_empty() {
        let capacity = (state.trade_qty + rules.qty).max(rules.qty);
        let best_queue = resting.iter().fold(0.0_f64, |best, order| {
            let ahead = queue_ahead(depth, order.side, order.px);
            best.max(capacity / (capacity + ahead))
        });
        rules.requote_tolerance = rules
            .requote_tolerance
            .max(micro.queue_reprice_edge * best_queue)
            .min(rules.half_spread * 0.95);
    }
    PricedQuote {
        fair_px,
        rules,
        protection,
    }
}

pub fn queue_ahead(depth: &Depth, side: Side, px: f64) -> f64 {
    match side {
        Side::Buy => depth.bids[..depth.bid_len as usize]
            .iter()
            .filter(|level| level.px >= px)
            .map(|level| level.qty)
            .sum(),
        Side::Sell => depth.asks[..depth.ask_len as usize]
            .iter()
            .filter(|level| level.px <= px)
            .map(|level| level.qty)
            .sum(),
    }
}

/// Apply the maker cap and the venue tick in the same pure contract research
/// calls. The engine quantizes once more at its boundary to the same value.
pub fn executable_quote_px(side: Side, wanted: f64, bid: f64, ask: f64, tick: f64) -> f64 {
    let passive = match side {
        Side::Buy => wanted.min(ask - tick),
        Side::Sell => wanted.max(bid + tick),
    };
    if !passive.is_finite() || !tick.is_finite() || tick <= 0.0 {
        return passive;
    }
    let steps = engine_types::quantize::steps(passive, tick);
    let snapped = match side {
        Side::Buy => steps.floor(),
        Side::Sell => steps.ceil(),
    };
    engine_types::quantize::round_clean(snapped * tick, tick)
}

/// One of this strategy's quotes that is currently working.
#[derive(Clone, Debug, PartialEq)]
pub struct Resting {
    pub client_order_id: String,
    pub side: Side,
    pub px: f64,
}

/// What to do about one side of the market.
#[derive(Clone, Debug, PartialEq)]
pub enum QuoteStep {
    /// Nothing is resting on this side and one should be.
    Place {
        side: Side,
        px: f64,
        qty: f64,
        stop_px: f64,
    },
    /// The resting quote is too far from where it belongs.
    Move { client_order_id: String, px: f64 },
    /// The quote should not be in the market at all: inventory is full on
    /// this side, or there is no usable price.
    Pull { client_order_id: String },
}

/// Extra distance or a complete withdrawal on the side current flow is
/// attacking. The untouched side keeps the ordinary quote exactly.
#[derive(Copy, Clone, Debug, Default, PartialEq)]
pub struct SideProtection {
    pub bid_extra: f64,
    pub ask_extra: f64,
    pub pull: Option<Side>,
}

/// Where the quotes are centred, given what is already held.
///
/// Long pushes the centre down, short pushes it up — so the side that would
/// add to the position quotes further away and the side that would reduce it
/// quotes closer. `position` is signed, positive long, and the lean is capped
/// at the ceiling so a position somehow past it cannot send the centre through
/// the floor.
pub fn centre(mid: f64, position: f64, rules: QuoteRules) -> f64 {
    if rules.skew <= 0.0 || rules.max_position <= 0.0 || !position.is_finite() {
        return mid;
    }
    let lean = (position / rules.max_position).clamp(-1.0, 1.0);
    mid * (1.0 - lean * rules.skew)
}

/// Work out what each side needs. `position` is signed: positive is long.
///
/// A crossed or empty book yields no quotes and pulls what is resting — a
/// price that is not a price is not something to quote around, and staying in
/// the market on a broken feed is how a maker gets picked off.
pub fn plan_quotes(
    bid_px: f64,
    ask_px: f64,
    position: f64,
    resting: &[Resting],
    rules: QuoteRules,
) -> Vec<QuoteStep> {
    plan_quotes_at(
        bid_px,
        ask_px,
        (bid_px + ask_px) / 2.0,
        position,
        resting,
        rules,
    )
}

/// The same order decision around an externally estimated fair price. The
/// live quoter supplies this from depth and trades; the plain wrapper above
/// keeps the original midpoint contract for simple callers and replay tests.
pub fn plan_quotes_at(
    bid_px: f64,
    ask_px: f64,
    fair_px: f64,
    position: f64,
    resting: &[Resting],
    rules: QuoteRules,
) -> Vec<QuoteStep> {
    plan_quotes_protected(
        bid_px,
        ask_px,
        fair_px,
        position,
        resting,
        rules,
        SideProtection::default(),
    )
}

/// Quote around `fair_px`, with an optional one-sided response to public
/// flow. Positive buy flow attacks the ask; sell flow attacks the bid. This
/// shape avoids paying for protection on the side the evidence does not say
/// is dangerous.
#[allow(clippy::too_many_arguments)]
pub fn plan_quotes_protected(
    bid_px: f64,
    ask_px: f64,
    fair_px: f64,
    position: f64,
    resting: &[Resting],
    rules: QuoteRules,
    protection: SideProtection,
) -> Vec<QuoteStep> {
    let mut steps = Vec::new();
    let usable =
        bid_px > 0.0 && ask_px > 0.0 && ask_px >= bid_px && fair_px.is_finite() && fair_px > 0.0;
    let mid = (bid_px + ask_px) / 2.0;
    let centre = centre(fair_px, position, rules);

    for side in [Side::Buy, Side::Sell] {
        let working = resting.iter().find(|r| r.side == side);
        // Quoting a side that would take the position past its ceiling is how
        // a maker ends up long a falling market: the side that keeps filling
        // is exactly the side that should stop.
        let would_exceed = match side {
            Side::Buy => position + rules.qty > rules.max_position,
            Side::Sell => position - rules.qty < -rules.max_position,
        };
        if !usable || would_exceed || protection.pull == Some(side) {
            if let Some(order) = working {
                steps.push(QuoteStep::Pull {
                    client_order_id: order.client_order_id.clone(),
                });
            }
            continue;
        }

        let want_px = match side {
            Side::Buy => centre * (1.0 - rules.half_spread - protection.bid_extra.max(0.0)),
            Side::Sell => centre * (1.0 + rules.half_spread + protection.ask_extra.max(0.0)),
        };
        match working {
            None => steps.push(QuoteStep::Place {
                side,
                px: want_px,
                qty: rules.qty,
                stop_px: quote_stop_px(want_px, side, rules.stop_loss_fraction),
            }),
            Some(order) => {
                let drift = (order.px - want_px).abs() / mid;
                if drift > rules.requote_tolerance {
                    steps.push(QuoteStep::Move {
                        client_order_id: order.client_order_id.clone(),
                        px: want_px,
                    });
                }
            }
        }
    }
    steps
}

pub fn quote_stop_px(px: f64, side: Side, fraction: f64) -> f64 {
    match side {
        Side::Buy => px * (1.0 - fraction),
        Side::Sell => px * (1.0 + fraction),
    }
}

#[cfg(test)]
mod tests {
    pub(super) use super::*;

    /// No skew, so the existing rules read exactly as they always did: the
    /// quotes sit symmetrically around mid whatever is held.
    pub(super) const RULES: QuoteRules = QuoteRules {
        half_spread: 0.001,
        requote_tolerance: 0.0005,
        qty: 1.0,
        max_position: 3.0,
        stop_loss_fraction: 0.35,
        skew: 0.0,
    };

    /// The same book, leaning against inventory: at the ceiling the centre
    /// moves a full half-spread, so one side of the quote lands on mid.
    pub(super) const LEANING: QuoteRules = QuoteRules {
        skew: 0.001,
        ..RULES
    };

    fn resting(id: &str, side: Side, px: f64) -> Resting {
        Resting {
            client_order_id: id.into(),
            side,
            px,
        }
    }

    #[test]
    fn an_empty_book_side_quotes_both_sides_around_mid() {
        let steps = plan_quotes(99.0, 101.0, 0.0, &[], RULES);
        assert_eq!(
            steps,
            vec![
                QuoteStep::Place {
                    side: Side::Buy,
                    px: 99.9,
                    qty: 1.0,
                    stop_px: 99.9 * 0.65
                },
                QuoteStep::Place {
                    side: Side::Sell,
                    px: 100.1,
                    qty: 1.0,
                    stop_px: 100.1 * 1.35
                },
            ]
        );
    }

    #[test]
    fn a_quote_that_is_nearly_right_is_left_where_it_is() {
        // Moving costs a request and the queue position it already earned.
        let steps = plan_quotes(
            99.0,
            101.0,
            0.0,
            &[
                resting("b", Side::Buy, 99.91),
                resting("a", Side::Sell, 100.09),
            ],
            RULES,
        );
        assert!(steps.is_empty());
    }

    #[test]
    fn a_quote_that_has_drifted_too_far_is_moved() {
        let steps = plan_quotes(99.0, 101.0, 0.0, &[resting("b", Side::Buy, 99.0)], RULES);
        assert_eq!(
            steps[0],
            QuoteStep::Move {
                client_order_id: "b".into(),
                px: 99.9
            }
        );
    }

    #[test]
    fn the_side_that_would_break_the_inventory_ceiling_stops_being_quoted() {
        // Long 3 with a ceiling of 3: buying more is what turns a maker into
        // a position.
        let steps = plan_quotes(99.0, 101.0, 3.0, &[], RULES);
        assert_eq!(steps.len(), 1);
        assert!(matches!(
            steps[0],
            QuoteStep::Place {
                side: Side::Sell,
                ..
            }
        ));
    }

    #[test]
    fn a_resting_quote_on_a_full_side_is_pulled() {
        let steps = plan_quotes(99.0, 101.0, 3.0, &[resting("b", Side::Buy, 99.9)], RULES);
        assert!(steps.contains(&QuoteStep::Pull {
            client_order_id: "b".into()
        }));
    }

    #[test]
    fn the_short_side_has_its_own_ceiling() {
        let steps = plan_quotes(99.0, 101.0, -3.0, &[], RULES);
        assert_eq!(steps.len(), 1);
        assert!(matches!(
            steps[0],
            QuoteStep::Place {
                side: Side::Buy,
                ..
            }
        ));
    }

    #[test]
    fn an_empty_book_pulls_everything_and_quotes_nothing() {
        // A zero is not a price. Quoting around one, or leaving a quote out
        // while the feed is broken, is how a maker gets picked off.
        let steps = plan_quotes(
            0.0,
            0.0,
            0.0,
            &[
                resting("b", Side::Buy, 99.9),
                resting("a", Side::Sell, 100.1),
            ],
            RULES,
        );
        assert_eq!(
            steps,
            vec![
                QuoteStep::Pull {
                    client_order_id: "b".into()
                },
                QuoteStep::Pull {
                    client_order_id: "a".into()
                },
            ]
        );
    }

    #[test]
    fn a_crossed_book_is_not_quoted_around() {
        let steps = plan_quotes(101.0, 99.0, 0.0, &[], RULES);
        assert!(steps.is_empty());
    }

    #[test]
    fn every_quote_carries_a_stop_because_the_kernel_demands_one() {
        let steps = plan_quotes(99.0, 101.0, 0.0, &[], RULES);
        for step in &steps {
            if let QuoteStep::Place {
                side, px, stop_px, ..
            } = step
            {
                match side {
                    Side::Buy => assert!(stop_px < px, "a long stop sits below"),
                    Side::Sell => assert!(stop_px > px, "a short stop sits above"),
                }
            }
        }
    }

    #[test]
    fn one_sided_protection_leaves_the_other_quote_exactly_alone() {
        let ordinary = plan_quotes_at(99.0, 101.0, 100.0, 0.0, &[], RULES);
        let protected = plan_quotes_protected(
            99.0,
            101.0,
            100.0,
            0.0,
            &[],
            RULES,
            SideProtection {
                ask_extra: 0.0004,
                ..SideProtection::default()
            },
        );
        let px = |steps: &[QuoteStep], side| {
            steps.iter().find_map(|step| match step {
                QuoteStep::Place { side: got, px, .. } if *got == side => Some(*px),
                _ => None,
            })
        };
        assert_eq!(px(&ordinary, Side::Buy), px(&protected, Side::Buy));
        assert!(px(&protected, Side::Sell) > px(&ordinary, Side::Sell));
    }

    #[test]
    fn a_pulled_side_is_removed_while_the_other_keeps_quoting() {
        let steps = plan_quotes_protected(
            99.0,
            101.0,
            100.0,
            0.0,
            &[],
            RULES,
            SideProtection {
                pull: Some(Side::Sell),
                ..SideProtection::default()
            },
        );
        assert_eq!(steps.len(), 1);
        assert!(matches!(
            steps[0],
            QuoteStep::Place {
                side: Side::Buy,
                ..
            }
        ));
    }
}

#[cfg(test)]
mod skew_tests {
    use super::tests::{LEANING, RULES};
    use super::*;

    fn px_of(steps: &[QuoteStep], want: Side) -> f64 {
        steps
            .iter()
            .find_map(|s| match s {
                QuoteStep::Place { side, px, .. } if *side == want => Some(*px),
                _ => None,
            })
            .unwrap_or_else(|| panic!("no {want:?} quote in {steps:?}"))
    }

    #[test]
    fn a_flat_book_quotes_evenly_around_the_market() {
        assert_eq!(centre(100.0, 0.0, LEANING), 100.0);
    }

    #[test]
    fn being_long_moves_both_quotes_down() {
        // The whole idea: the side that would sell us out of the position gets
        // cheaper to hit, and the side that would add to it gets dearer. Both
        // move, which is what makes it a lean rather than a one-sided pull.
        let flat = plan_quotes(99.0, 101.0, 0.0, &[], LEANING);
        let long = plan_quotes(99.0, 101.0, 1.5, &[], LEANING);
        assert!(
            px_of(&long, Side::Buy) < px_of(&flat, Side::Buy),
            "bid backs off"
        );
        assert!(
            px_of(&long, Side::Sell) < px_of(&flat, Side::Sell),
            "ask comes in"
        );
    }

    #[test]
    fn being_short_moves_both_quotes_up() {
        let flat = plan_quotes(99.0, 101.0, 0.0, &[], LEANING);
        let short = plan_quotes(99.0, 101.0, -1.5, &[], LEANING);
        assert!(px_of(&short, Side::Buy) > px_of(&flat, Side::Buy));
        assert!(px_of(&short, Side::Sell) > px_of(&flat, Side::Sell));
    }

    #[test]
    fn the_lean_is_proportional_and_stops_at_the_ceiling() {
        // Half-full leans half as far as full, and a position somehow past the
        // ceiling cannot lean further than full — otherwise a stale reading
        // could send the centre through the floor.
        let mid = 100.0;
        let half = mid - centre(mid, 1.5, LEANING);
        let full = mid - centre(mid, 3.0, LEANING);
        let over = mid - centre(mid, 30.0, LEANING);
        assert!((full - 2.0 * half).abs() < 1e-12, "{full} vs {half}");
        assert_eq!(over, full, "capped at the ceiling");
    }

    #[test]
    fn no_skew_is_the_old_behaviour_exactly() {
        // The dial off must be the strategy that existed before it, or every
        // number measured against the old one is invalidated by a default.
        for position in [-3.0, -1.0, 0.0, 1.0, 3.0] {
            assert_eq!(centre(100.0, position, RULES), 100.0);
        }
    }

    #[test]
    fn a_ceiling_still_stops_a_side_outright() {
        // The lean is continuous pressure, not a limit. At the ceiling the
        // side that would add to the position stops being quoted at all.
        let steps = plan_quotes(99.0, 101.0, 3.0, &[], LEANING);
        assert_eq!(steps.len(), 1);
        assert!(matches!(
            steps[0],
            QuoteStep::Place {
                side: Side::Sell,
                ..
            }
        ));
    }

    #[test]
    fn a_nonsense_position_leaves_the_centre_alone() {
        assert_eq!(centre(100.0, f64::NAN, LEANING), 100.0);
        assert_eq!(
            centre(
                100.0,
                1.0,
                QuoteRules {
                    max_position: 0.0,
                    ..LEANING
                }
            ),
            100.0
        );
    }
}
