//! Where a worked entry should sit, and when it should stop being patient.
//!
//! Pure arithmetic, like the quoter's planner: the touch, the clock, the
//! order's own price and counters, and the policy are all arguments, so every
//! rule here can be read off a test instead of out of a running engine.
//!
//! The recipe's numbers come from a 34-symbol night replay of recorded books
//! (199,785 paired attempts, fill
//! bounds that respect queue position). It measured 0.36 bp per entry cheaper
//! than plainly joining the touch and repricing on a timer, with deadline
//! crosses halved.

use engine_types::quantize::quantize_px;
use engine_types::{InstrumentRule, Intent, OrderKind, Side, WorkPolicy};

/// Below two ticks of spread, resting stops paying: the taker cost is already
/// near the maker floor, and the night replay showed tight-spread books losing
/// to adverse selection — the order fills exactly when the price is about to
/// move against it.
pub const MIN_SPREAD_TICKS: f64 = 2.0;

/// The same floor as a share of price, for instruments whose tick is so fine
/// that two ticks is nothing.
pub const MIN_SPREAD_FRACTION: f64 = 0.0001;

/// The touch, with the displayed sizes when the feed carries them.
///
/// A size of zero or less means the sizes are not known. Every rule that reads
/// the lean then falls back to plainly joining the touch.
#[derive(Copy, Clone, Debug, Default, PartialEq)]
pub struct Touch {
    pub bid_px: f64,
    pub bid_qty: f64,
    pub ask_px: f64,
    pub ask_qty: f64,
}

impl Touch {
    /// A two-sided book with prices in the right order. Anything else is not
    /// something to quote around.
    pub fn readable(&self) -> bool {
        self.bid_px.is_finite()
            && self.ask_px.is_finite()
            && self.bid_px > 0.0
            && self.ask_px > self.bid_px
    }

    fn mid(&self) -> f64 {
        (self.bid_px + self.ask_px) / 2.0
    }

    fn spread(&self) -> f64 {
        self.ask_px - self.bid_px
    }

    /// The side this order joins, and the far side it would have to cross.
    fn near(&self, side: Side) -> f64 {
        match side {
            Side::Buy => self.bid_px,
            Side::Sell => self.ask_px,
        }
    }

    fn far(&self, side: Side) -> f64 {
        match side {
            Side::Buy => self.ask_px,
            Side::Sell => self.bid_px,
        }
    }
}

/// One worked order's own price, clock, and counters. All plain numbers, so
/// the decision below can be driven straight from a test.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct WorkState {
    pub side: Side,
    /// Where it is resting right now.
    pub px: f64,
    /// When it went out, which is where the window is measured from.
    pub placed_ns: u64,
    /// The last pass that spent a look on it, whether or not the book could
    /// be read.
    pub last_look_ns: u64,
    pub last_cross_try_ns: u64,
    pub last_cancel_try_ns: u64,
    /// When the cross grace started. Zero while the order is still patient.
    pub cross_started_ns: u64,
    /// A crossing amend the venue took. An attempt it refused does not count:
    /// the order is still resting at its old passive price.
    pub crossed: bool,
    /// A cancel the venue took.
    pub cancel_requested: bool,
    /// Moves the venue took.
    pub amends: u32,
    /// The mid when the order was decided. Zero when it is not known, and the
    /// early cross then stays off.
    pub decision_mid: f64,
}

impl WorkState {
    pub fn new(side: Side, px: f64, decision_mid: f64, now_ns: u64) -> Self {
        WorkState {
            side,
            px,
            placed_ns: now_ns,
            last_look_ns: now_ns,
            last_cross_try_ns: 0,
            last_cancel_try_ns: 0,
            cross_started_ns: 0,
            crossed: false,
            cancel_requested: false,
            amends: 0,
            decision_mid: if decision_mid.is_finite() && decision_mid > 0.0 {
                decision_mid
            } else {
                0.0
            },
        }
    }
}

/// What to do about one worked order on this pass.
#[derive(Copy, Clone, Debug, PartialEq)]
pub enum WorkStep {
    /// Leave it where it is.
    Hold,
    /// Move it, keeping its venue identity and its place in the queue.
    Move { px: f64 },
    /// Take the far touch at a bounded price.
    Cross { px: f64 },
    /// It is time to cross but the book cannot be read to price one. The
    /// grace clock starts anyway, so the cancel still fires on time and the
    /// next pass can retry the cross.
    CrossUnpriced,
    /// The cross could not clear the rest: pull the order.
    Cancel,
}

/// The step, and whether the reprice cadence came round on this pass.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct WorkDecision {
    pub step: WorkStep,
    /// The supervisor stamps the look when this is true — including when the
    /// book turned out to be unreadable. A dark symbol that did not stamp
    /// would be retried on every single tick instead of on the cadence.
    pub looked: bool,
}

impl WorkDecision {
    const fn hold() -> Self {
        WorkDecision {
            step: WorkStep::Hold,
            looked: false,
        }
    }

    const fn looked(step: WorkStep) -> Self {
        WorkDecision { step, looked: true }
    }
}

/// What the engine should do with an intent whose strategy asked to have it
/// worked.
#[derive(Copy, Clone, Debug, PartialEq)]
pub enum Opening {
    /// Send it exactly as the strategy wrote it, and work nothing. This is
    /// every order that carries no policy, every exit, and every entry whose
    /// book is too tight for resting to pay.
    AsWritten,
    /// Send it as a limit resting here, and work it.
    Rest { px: f64, policy: WorkPolicy },
    /// The strategy priced it itself. Leave the price alone and work it from
    /// there.
    WorkAsPriced { policy: WorkPolicy },
}

/// The displayed size split at the touch, from this order's point of view.
///
/// Positive means the book leans toward filling us late — there is size ahead
/// of us and the price is about to walk away. Negative means the touch we want
/// to join is about to trade, and joining it buys the adverse fill. `None`
/// when the feed carries no sizes.
pub fn lean(side: Side, touch: Touch) -> Option<f64> {
    let (bid, ask) = (touch.bid_qty, touch.ask_qty);
    if !bid.is_finite() || !ask.is_finite() || bid < 0.0 || ask < 0.0 {
        return None;
    }
    let total = bid + ask;
    if !total.is_finite() || total <= 0.0 {
        return None;
    }
    let share = bid / total;
    Some(match side {
        Side::Buy => share - 0.5,
        Side::Sell => 0.5 - share,
    })
}

/// Is there enough spread here for resting to beat crossing?
pub fn worth_resting(touch: Touch, tick: f64) -> bool {
    if tick <= 0.0 || !touch.readable() {
        return false;
    }
    touch.spread() >= MIN_SPREAD_TICKS * tick && touch.spread() / touch.mid() >= MIN_SPREAD_FRACTION
}

/// Read an intent and say how it should go out.
///
/// An exit is never rested. A resting exit that does not fill is exposure
/// nobody wanted, still on the book — so exits cross and entries rest, which
/// is how every sleeve in this system works.
pub fn opening(intent: &Intent, touch: Touch, rule: &InstrumentRule) -> Opening {
    let Some(policy) = intent.work else {
        return Opening::AsWritten;
    };
    if intent.reduce_only {
        return Opening::AsWritten;
    }
    match intent.kind {
        // The strategy named its own price. Second-guessing where it wanted
        // to sit would be a different trade, so the supervisor works it from
        // wherever the strategy put it.
        OrderKind::Limit { .. } => Opening::WorkAsPriced { policy },
        OrderKind::Market => match resting_px(intent.side, touch, rule, &policy) {
            Some(px) => Opening::Rest { px, policy },
            None => Opening::AsWritten,
        },
    }
}

/// Where a fresh entry should rest, or `None` when resting cannot help and the
/// order should go as written.
pub fn resting_px(
    side: Side,
    touch: Touch,
    rule: &InstrumentRule,
    policy: &WorkPolicy,
) -> Option<f64> {
    let tick = rule.tick_size;
    if !worth_resting(touch, tick) {
        return None;
    }
    if policy.hold_decision_px {
        // This book's mid IS the decision mid: both are read from the same
        // quote in the same pass. Resting there is inside the touch, which
        // the spread gate above leaves room for, and it is the cap the order
        // never moves past afterwards.
        return Some(snap_rest(touch.mid(), side, rule));
    }
    let lean = lean(side, touch);
    if policy.improve_lean > 0.0 && lean.is_some_and(|l| l >= policy.improve_lean) {
        // The spread gate above is what makes this safe: with two ticks to
        // work with, one tick inside is still a maker.
        return Some(snap_rest(inside(side, touch, tick), side, rule));
    }
    if policy.back_lean > 0.0 && lean.is_some_and(|l| l <= -policy.back_lean) {
        let behind = match side {
            Side::Buy => touch.bid_px - tick,
            Side::Sell => touch.ask_px + tick,
        };
        if behind > 0.0 {
            return Some(snap_rest(behind, side, rule));
        }
    }
    Some(snap_rest(touch.near(side), side, rule))
}

/// One pass over one worked order.
///
/// The branch order is the whole decision and is not free to change: a
/// crossing order must never be repriced back to a passive price, and a
/// window that has run out must not wait for the reprice cadence.
pub fn plan_work(
    state: &WorkState,
    touch: Touch,
    rule: &InstrumentRule,
    now_ns: u64,
    policy: &WorkPolicy,
) -> WorkDecision {
    if rule.tick_size <= 0.0 {
        return WorkDecision::hold();
    }
    // The venue took a cancel, so this order is on its way out whichever path
    // asked for it.
    if state.cancel_requested {
        return WorkDecision::hold();
    }
    let reprice_ns = ms_ns(policy.reprice_ms);

    // Already crossing. Nothing here is patient any more; the only questions
    // are whether to retry the cross and when to give up on it.
    if state.cross_started_ns != 0 {
        let grace_over = now_ns
            >= state
                .cross_started_ns
                .saturating_add(ms_ns(policy.cross_grace_ms));
        if !state.crossed && !grace_over {
            // Paced, and that pacing is load-bearing. Each attempt is a
            // signed venue call at roughly 175 ms; retrying on every pass
            // spent the whole grace window blocking the loop and tripped the
            // venue's order-rate limit.
            if now_ns.saturating_sub(state.last_cross_try_ns) < reprice_ns {
                return WorkDecision::hold();
            }
            return WorkDecision::looked(cross_step(state.side, touch, rule));
        }
        if grace_over {
            // The bounded cross could not clear the rest: take the order down
            // and let the strategy decide again. Paced for the same reason,
            // so a venue that keeps refusing cannot become a hot loop.
            if now_ns.saturating_sub(state.last_cancel_try_ns) < reprice_ns {
                return WorkDecision::hold();
            }
            return WorkDecision::looked(WorkStep::Cancel);
        }
        return WorkDecision::hold();
    }

    // The window ran out: stop paying for patience.
    if now_ns >= state.placed_ns.saturating_add(ms_ns(policy.window_ms)) {
        return patience_over(state, touch, rule, now_ns, policy);
    }

    // The reprice gate. Everything past this line counts as a look, book
    // readable or not.
    if now_ns.saturating_sub(state.last_look_ns) < reprice_ns {
        return WorkDecision::hold();
    }
    if !touch.readable() {
        return WorkDecision::looked(WorkStep::Hold);
    }

    // The market has already run past the price of crossing, so waiting has
    // been proven more expensive than the spread. Only when we know what the
    // mid was when the order was decided.
    if policy.drift_cross_fee_bp > 0.0 && state.decision_mid > 0.0 {
        let mid = touch.mid();
        let drift_bp = (mid - state.decision_mid) / state.decision_mid * 1e4;
        let against_bp = match state.side {
            Side::Buy => drift_bp,
            Side::Sell => -drift_bp,
        };
        let half_spread_bp = touch.spread() / 2.0 / mid * 1e4;
        if against_bp >= 2.0 * (half_spread_bp + policy.drift_cross_fee_bp) {
            return patience_over(state, touch, rule, now_ns, policy);
        }
    }

    let Some(want) = desired_px(state, touch, rule, now_ns, policy) else {
        return WorkDecision::looked(WorkStep::Hold);
    };
    // Below half a tick it is the same price, and moving would give up queue
    // position for nothing.
    if (want - state.px).abs() < rule.tick_size * 0.5 {
        return WorkDecision::looked(WorkStep::Hold);
    }
    // The budget is a schedule, not a protection: eight moves at the reprice
    // cadence span the whole window. Past the join threshold, escalation wins
    // anyway.
    if state.amends >= policy.max_amends
        && elapsed_frac(state, now_ns, policy) < policy.urgency_join_frac
    {
        return WorkDecision::looked(WorkStep::Hold);
    }
    WorkDecision::looked(WorkStep::Move { px: want })
}

/// Patience is over, one way or the other: cross for whatever is left, or take
/// the order down and let the strategy decide again.
///
/// The cancel is paced like every other venue call here, so a venue that keeps
/// refusing one cannot become a hot loop.
fn patience_over(
    state: &WorkState,
    touch: Touch,
    rule: &InstrumentRule,
    now_ns: u64,
    policy: &WorkPolicy,
) -> WorkDecision {
    if !policy.give_up_instead_of_crossing {
        return WorkDecision::looked(cross_step(state.side, touch, rule));
    }
    if now_ns.saturating_sub(state.last_cancel_try_ns) < ms_ns(policy.reprice_ms) {
        return WorkDecision::hold();
    }
    WorkDecision::looked(WorkStep::Cancel)
}

/// Never worse than the price the order was decided at, when the policy says
/// so. A buy is capped from above and a sell from below, so this only ever
/// moves a price toward the passive side — it can never turn a resting price
/// into a crossing one.
fn capped(px: f64, state: &WorkState, policy: &WorkPolicy) -> f64 {
    if !policy.hold_decision_px || state.decision_mid <= 0.0 {
        return px;
    }
    match state.side {
        Side::Buy => px.min(state.decision_mid),
        Side::Sell => px.max(state.decision_mid),
    }
}

/// How far through its window this order is, from 0 at placement to 1 at the
/// end.
pub fn elapsed_frac(state: &WorkState, now_ns: u64, policy: &WorkPolicy) -> f64 {
    let window_ns = ms_ns(policy.window_ms);
    if window_ns == 0 {
        return 1.0;
    }
    (now_ns.saturating_sub(state.placed_ns) as f64 / window_ns as f64).clamp(0.0, 1.0)
}

/// Where a resting order belongs on a later pass, or `None` to leave it.
///
/// The governing rule: a touch that fell away left our order alone at the
/// front of the book, which is the best place to be — so never chase toward a
/// retreating market. Everything else escalates: rejoin when overtaken, jump
/// the queue when the book leans our way and there is spread to jump into,
/// never rest behind the touch past half the window, improve outright near
/// the end.
fn desired_px(
    state: &WorkState,
    touch: Touch,
    rule: &InstrumentRule,
    now_ns: u64,
    policy: &WorkPolicy,
) -> Option<f64> {
    let tick = rule.tick_size;
    let near = touch.near(state.side);
    let can_improve = touch.spread() >= MIN_SPREAD_TICKS * tick;
    let improved = inside(state.side, touch, tick);
    let elapsed = elapsed_frac(state, now_ns, policy);
    let lean = lean(state.side, touch);
    let half = tick * 0.5;
    // "Ahead" is inside the touch — a buy priced above the bid. "Behind" is
    // worse than the touch, which means the market overtook us.
    let (ahead, behind) = match state.side {
        Side::Buy => (state.px > near + half, state.px < near - half),
        Side::Sell => (state.px < near - half, state.px > near + half),
    };
    let wants_improve =
        policy.improve_lean > 0.0 && lean.is_some_and(|l| l >= policy.improve_lean) && can_improve;

    if elapsed >= policy.urgency_improve_frac && can_improve && !ahead {
        return Some(snap_rest(capped(improved, state, policy), state.side, rule));
    }
    if behind {
        let stay_back = policy.back_lean > 0.0
            && lean.is_some_and(|l| l <= -policy.back_lean)
            && elapsed < policy.urgency_join_frac;
        if stay_back {
            return None;
        }
        let want = if wants_improve { improved } else { near };
        return Some(snap_rest(capped(want, state, policy), state.side, rule));
    }
    if !ahead && wants_improve {
        return Some(snap_rest(capped(improved, state, policy), state.side, rule));
    }
    None
}

/// Amend through the far touch with a pad, so the rest fills as a taker at a
/// price we chose — unlike a market order, which fills at whatever is there.
fn cross_step(side: Side, touch: Touch, rule: &InstrumentRule) -> WorkStep {
    if !touch.readable() {
        return WorkStep::CrossUnpriced;
    }
    let tick = rule.tick_size;
    let far = touch.far(side);
    let pad = (tick * MIN_SPREAD_TICKS).max(far * MIN_SPREAD_FRACTION);
    let raw = match side {
        Side::Buy => far + pad,
        Side::Sell => (far - pad).max(tick),
    };
    // Snapped the aggressive way round — a buy rounds up here, the opposite
    // of a resting price — because a crossing order that got rounded back
    // inside the spread has not crossed.
    WorkStep::Cross {
        px: quantize_px(raw, side.flipped(), rule),
    }
}

/// One tick inside the spread.
fn inside(side: Side, touch: Touch, tick: f64) -> f64 {
    match side {
        Side::Buy => touch.bid_px + tick,
        Side::Sell => touch.ask_px - tick,
    }
}

/// Snap a resting price to the instrument grid, away from the spread: a buy
/// rounds down, a sell rounds up, so snapping can never turn a passive price
/// into a crossing one.
fn snap_rest(px: f64, side: Side, rule: &InstrumentRule) -> f64 {
    quantize_px(px, side, rule)
}

fn ms_ns(ms: u64) -> u64 {
    ms.saturating_mul(1_000_000)
}

#[cfg(test)]
mod tests;
