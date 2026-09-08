//! Hypothetical fills use displayed queue and finite public trade volume, never book touches.

use engine_core::working::plan::{self, Touch, WorkState, WorkStep};
use engine_types::{Depth, InstrumentRule, OrderKind, Side, TimeInForce, WorkPolicy};
use serde::{Deserialize, Serialize};

use super::{ObservedOrder, Result};

const NS_MS: u64 = 1_000_000;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Policy {
    Cross,
    Current,
    #[serde(rename = "passive_entry_30s")]
    PassiveEntry30s,
    PostOnly5s,
    PostOnly30s,
    PostOnly120s,
    Adaptive120s,
    PassiveSkip120s,
}

impl Policy {
    pub fn name(self) -> &'static str {
        match self {
            Self::Cross => "cross",
            Self::Current => "current",
            Self::PassiveEntry30s => "passive_entry_30s",
            Self::PostOnly5s => "post_only_5s",
            Self::PostOnly30s => "post_only_30s",
            Self::PostOnly120s => "post_only_120s",
            Self::Adaptive120s => "adaptive_120s",
            Self::PassiveSkip120s => "passive_skip_120s",
        }
    }
    fn native_work(self) -> bool {
        matches!(self, Self::Current | Self::PassiveEntry30s)
    }

    fn window_ns(self) -> u64 {
        (match self {
            Self::Cross => 0,
            Self::PostOnly5s => 5_000,
            Self::PostOnly30s => 30_000,
            _ => 120_000,
        }) * NS_MS
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QueueModel {
    TradesOnly,
    CancellationsAhead,
}

#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub struct Fees {
    pub maker: f64,
    pub taker: f64,
    pub observed_ns: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Fill {
    pub at_ns: u64,
    pub qty: f64,
    pub price: f64,
    pub maker: bool,
    pub fee: f64,
    pub signed_markouts_bp: [Option<f64>; 4],
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Outcome {
    pub policy: Policy,
    pub queue_model: QueueModel,
    pub hop_ms: u64,
    pub qty: f64,
    pub filled_qty: f64,
    pub maker_qty: f64,
    pub requested_notional: f64,
    pub fill_shortfall_bp: f64,
    pub fee_bp: f64,
    pub missed_opportunity_bp: Option<f64>,
    pub total_shortfall_bp: Option<f64>,
    pub mark_ns: Option<u64>,
    pub requests: u64,
    pub post_only_rejections: u64,
    pub public_trade_rows: u64,
    pub incomplete: Option<String>,
    pub fills: Vec<Fill>,
    pub decision_features: Option<DecisionFeatures>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct DecisionFeatures {
    pub book_ns: u64,
    pub bid: f64,
    pub ask: f64,
    pub bid_qty: f64,
    pub ask_qty: f64,
    pub spread_bp: f64,
    pub side_lean: Option<f64>,
    pub signed_trade_flow: f64,
}

#[derive(Clone, Copy)]
struct Resting {
    price: f64,
    queue: f64,
    displayed: f64,
    trades_since_book: f64,
}

#[derive(Clone, Copy)]
enum Action {
    Place { price: Option<f64>, post_only: bool },
    Cancel,
    Amend { price: f64 },
}

pub struct Trial {
    pub policy: Policy,
    pub queue_model: QueueModel,
    pub hop_ms: u64,
    pub start_ns: u64,
    pub mark_at_ns: u64,
    side: Side,
    qty: f64,
    anchor: f64,
    rule: InstrumentRule,
    fees: Fees,
    prewire_ns: u64,
    original: OrderKind,
    passive_entry_candidate: bool,
    work_policy: Option<WorkPolicy>,
    work: Option<WorkState>,
    resting: Option<Resting>,
    pending: Vec<(u64, Action)>,
    next_look_ns: u64,
    started: bool,
    deadline_started: bool,
    finished: bool,
    fills: Vec<Fill>,
    requests: u64,
    rejects: u64,
    incomplete: Option<String>,
    mark: Option<(u64, f64)>,
    signed_flow: f64,
    flow_ns: u64,
    advanced_ns: u64,
    trade_rows: u64,
    decision_features: Option<DecisionFeatures>,
}

impl Trial {
    pub fn new(
        order: &ObservedOrder,
        policy: Policy,
        queue_model: QueueModel,
        hop_ms: u64,
        fees: Fees,
    ) -> Result<Self> {
        let start = order
            .decision_ns
            .ok_or("order has no aligned decision clock")?;
        let rule = order
            .rule
            .ok_or("order has no contemporaneous instrument rule")?;
        if !order.symbol.ends_with("USDT") {
            return Err("execution study currently prices USDT linear contracts only".into());
        }
        if hop_ms == 0
            || order.request.qty <= 0.0
            || !order.request.qty.is_finite()
            || !order.arrival_mid.is_finite()
            || order.arrival_mid <= 0.0
            || rule.tick_size <= 0.0
            || !rule.tick_size.is_finite()
            || !fees.maker.is_finite()
            || !fees.taker.is_finite()
        {
            return Err("invalid trial quantity, anchor, instrument or fees".into());
        }
        let candidate = policy == Policy::PassiveEntry30s
            && order.sleeve == "long"
            && !order.request.reduce_only;
        Ok(Self {
            policy,
            queue_model,
            hop_ms,
            start_ns: start,
            mark_at_ns: start + 180_000 * NS_MS,
            side: order.request.side,
            qty: order.request.qty,
            anchor: order.arrival_mid,
            rule,
            fees,
            prewire_ns: order
                .socket_write_ns
                .and_then(|at| at.checked_sub(start))
                .unwrap_or_default(),
            original: if candidate {
                OrderKind::Market
            } else {
                order.request.kind
            },
            passive_entry_candidate: candidate,
            work_policy: if candidate {
                Some(WorkPolicy::passive_entry_30s())
            } else {
                order.intent.as_ref().and_then(|i| i.work)
            },
            work: None,
            resting: None,
            pending: Vec::new(),
            next_look_ns: start,
            started: false,
            deadline_started: false,
            finished: false,
            fills: Vec::new(),
            requests: 0,
            rejects: 0,
            incomplete: None,
            mark: None,
            signed_flow: 0.0,
            flow_ns: 0,
            advanced_ns: 0,
            trade_rows: 0,
            decision_features: None,
        })
    }

    fn remaining(&self) -> f64 {
        (self.qty - self.fills.iter().map(|f| f.qty).sum::<f64>()).max(0.0)
    }
    pub fn next_wake(&self) -> Option<u64> {
        if !self.started {
            return Some(self.start_ns);
        }
        if self.finished {
            return None;
        }
        let pending = self.pending.first().map(|p| p.0);
        let mut wake = pending;
        if !self.deadline_started {
            let deadline = if self.policy.native_work() {
                self.work
                    .as_ref()
                    .zip(self.work_policy)
                    .map(|(w, p)| w.placed_ns + p.window_ms * NS_MS)
            } else if self.policy == Policy::Cross {
                None
            } else {
                Some(self.start_ns + self.policy.window_ns())
            };
            wake = wake
                .into_iter()
                .chain(deadline)
                .chain(Some(self.next_look_ns))
                .filter(|at| *at > self.advanced_ns)
                .min();
        }
        wake
    }
    fn hop(&self) -> u64 {
        self.hop_ms * NS_MS
    }
    fn fresh(book: Option<&Depth>, at: u64) -> Option<&Depth> {
        book.filter(|d| {
            d.bid_len > 0
                && d.ask_len > 0
                && d.bids[0].px < d.asks[0].px
                && d.recv_ns <= at
                && at - d.recv_ns <= 2_000 * NS_MS
        })
    }
    fn touch(d: &Depth) -> Touch {
        Touch {
            bid_px: d.bids[0].px,
            bid_qty: d.bids[0].qty,
            ask_px: d.asks[0].px,
            ask_qty: d.asks[0].qty,
        }
    }
    fn near(&self, d: &Depth) -> f64 {
        match self.side {
            Side::Buy => d.bids[0].px,
            Side::Sell => d.asks[0].px,
        }
    }
    fn displayed(&self, d: &Depth, price: f64) -> f64 {
        let levels = match self.side {
            Side::Buy => &d.bids[..d.bid_len as usize],
            Side::Sell => &d.asks[..d.ask_len as usize],
        };
        levels
            .iter()
            .find(|l| (l.px - price).abs() < self.rule.tick_size * 0.25)
            .map(|l| l.qty)
            .unwrap_or(0.0)
    }
    fn crossed(&self, d: &Depth, price: f64) -> bool {
        match self.side {
            Side::Buy => price >= d.asks[0].px,
            Side::Sell => price <= d.bids[0].px,
        }
    }
    fn add_fill(&mut self, at: u64, qty: f64, price: f64, maker: bool) {
        let qty = qty.min(self.remaining());
        if qty > 0.0 {
            self.fills.push(Fill {
                at_ns: at,
                qty,
                price,
                maker,
                fee: qty
                    * price
                    * if maker {
                        self.fees.maker
                    } else {
                        self.fees.taker
                    },
                signed_markouts_bp: [None; 4],
            });
        }
        if self.remaining() <= self.qty * 1e-12 {
            self.finished = true;
            self.resting = None;
            self.pending.clear();
        }
    }
    fn take(&mut self, at: u64, d: &Depth, limit: Option<f64>) {
        let levels = match self.side {
            Side::Buy => &d.asks[..d.ask_len as usize],
            Side::Sell => &d.bids[..d.bid_len as usize],
        };
        for level in levels {
            if limit.is_some_and(|p| match self.side {
                Side::Buy => level.px > p,
                Side::Sell => level.px < p,
            }) {
                break;
            }
            self.add_fill(at, level.qty, level.px, false);
            if self.finished {
                break;
            }
        }
    }
    fn place(&mut self, at: u64, d: &Depth, price: Option<f64>, post_only: bool) {
        if let Some(price) = price {
            if post_only && self.crossed(d, price) {
                self.rejects += 1;
                return;
            }
            if self.crossed(d, price) {
                self.take(at, d, Some(price));
            }
            if !self.finished {
                let displayed = self.displayed(d, price);
                self.resting = Some(Resting {
                    price,
                    queue: displayed,
                    displayed,
                    trades_since_book: 0.0,
                });
            }
        } else {
            self.take(at, d, None);
            if !self.finished {
                self.incomplete = Some("insufficient_displayed_depth".into());
                self.finished = true;
            }
        }
    }
    fn schedule(&mut self, at: u64, action: Action) {
        self.pending.push((at, action));
        self.pending.sort_by_key(|a| a.0);
        self.requests += 1;
    }
    fn adaptive_price(&self, d: &Depth, at: u64) -> f64 {
        let touch = Self::touch(d);
        let lean = plan::lean(self.side, touch).unwrap_or_default();
        let flow = self.signed_flow
            * (-((at.saturating_sub(self.flow_ns)) as f64) / (3_000.0 * NS_MS as f64)).exp();
        let attacked = match self.side {
            Side::Buy => -flow,
            Side::Sell => flow,
        };
        let near = self.near(d);
        let direction = if self.side == Side::Buy { 1.0 } else { -1.0 };
        if lean < -0.15 || attacked > 0.5 {
            near - direction * self.rule.tick_size
        } else if lean > 0.15 && d.asks[0].px - d.bids[0].px > 1.5 * self.rule.tick_size {
            near + direction * self.rule.tick_size
        } else {
            near
        }
    }

    /// Called before each tape row, with only the book already observed.
    pub fn advance(&mut self, at: u64, book: Option<&Depth>) {
        self.advanced_ns = at;
        if !self.started && at >= self.start_ns {
            self.started = true;
            let Some(d) = Self::fresh(book, self.start_ns) else {
                self.incomplete = Some("no_fresh_decision_book".into());
                self.finished = true;
                return;
            };
            let (price, post_only) = if self.policy == Policy::Cross {
                (None, false)
            } else if self.policy.native_work() {
                match self.original {
                    OrderKind::Market => (
                        if self.passive_entry_candidate {
                            self.work_policy.and_then(|p| {
                                plan::resting_px(self.side, Self::touch(d), &self.rule, &p)
                            })
                        } else {
                            None
                        },
                        false,
                    ),
                    OrderKind::Limit { px, tif } => (Some(px), tif == TimeInForce::PostOnly),
                }
            } else {
                (
                    Some(if self.policy == Policy::Adaptive120s {
                        self.adaptive_price(d, self.start_ns)
                    } else {
                        self.near(d)
                    }),
                    true,
                )
            };
            self.decision_features = Some(DecisionFeatures {
                book_ns: d.recv_ns,
                bid: d.bids[0].px,
                ask: d.asks[0].px,
                bid_qty: d.bids[0].qty,
                ask_qty: d.asks[0].qty,
                spread_bp: (d.asks[0].px - d.bids[0].px) / ((d.asks[0].px + d.bids[0].px) / 2.0)
                    * 1e4,
                side_lean: plan::lean(self.side, Self::touch(d)),
                signed_trade_flow: self.signed_flow
                    * (-((self.start_ns.saturating_sub(self.flow_ns)) as f64)
                        / (3_000.0 * NS_MS as f64))
                        .exp(),
            });
            let placement = self.start_ns + self.prewire_ns + self.hop();
            if self.policy.native_work() {
                if let Some(px) = price {
                    self.work = Some(WorkState::new(self.side, px, self.anchor, placement));
                }
            }
            self.schedule(placement, Action::Place { price, post_only });
            self.next_look_ns = if self.policy.native_work() {
                self.work_policy
                    .map(|p| placement + p.reprice_ms * NS_MS)
                    .unwrap_or(self.start_ns + 5_000 * NS_MS)
            } else {
                self.start_ns + 5_000 * NS_MS
            };
        }
        while !self.pending.is_empty() && self.pending[0].0 <= at && !self.finished {
            let (due, action) = self.pending.remove(0);
            if matches!(action, Action::Cancel) {
                self.resting = None;
                if self.pending.is_empty() {
                    self.finished = true;
                }
                continue;
            }
            let Some(d) = Self::fresh(book, due) else {
                self.incomplete = Some("no_fresh_book_at_request_arrival".into());
                self.finished = true;
                break;
            };
            match action {
                Action::Place { price, post_only } => self.place(due, d, price, post_only),
                Action::Amend { price } => {
                    self.resting = None;
                    self.place(due, d, Some(price), false);
                }
                Action::Cancel => {}
            }
        }
        if self.finished || !self.started {
            return;
        }
        let deadline = self.start_ns + self.policy.window_ns();
        if !self.policy.native_work()
            && !self.deadline_started
            && at >= deadline
            && self.policy != Policy::Cross
        {
            self.deadline_started = true;
            self.schedule(deadline + self.hop(), Action::Cancel);
            if self.policy != Policy::PassiveSkip120s {
                self.schedule(
                    deadline + 2 * self.hop(),
                    Action::Place {
                        price: None,
                        post_only: false,
                    },
                );
            }
            return;
        }
        let work_deadline = self
            .work
            .as_ref()
            .zip(self.work_policy)
            .is_some_and(|(w, p)| at >= w.placed_ns + p.window_ms * NS_MS);
        if (at < self.next_look_ns && !work_deadline)
            || !self.pending.is_empty()
            || self.deadline_started
        {
            return;
        }
        self.next_look_ns = at + 5_000 * NS_MS;
        let Some(d) = Self::fresh(book, at) else {
            return;
        };
        if self.policy.native_work() {
            if let (Some(work), Some(policy)) = (&mut self.work, self.work_policy) {
                let decision = plan::plan_work(work, Self::touch(d), &self.rule, at, &policy);
                if decision.looked {
                    work.last_look_ns = at;
                }
                match decision.step {
                    WorkStep::Move { px } => {
                        work.px = px;
                        work.amends += 1;
                        self.schedule(at + self.hop(), Action::Amend { price: px });
                    }
                    WorkStep::Cross { px } => {
                        work.crossed = true;
                        work.cross_started_ns = at;
                        self.deadline_started = true;
                        self.schedule(at + self.hop(), Action::Amend { price: px });
                        self.schedule(at + policy.cross_grace_ms * NS_MS, Action::Cancel);
                    }
                    WorkStep::Cancel => {
                        self.schedule(at + self.hop(), Action::Cancel);
                        self.deadline_started = true;
                    }
                    _ => {}
                }
            }
        } else if self.policy == Policy::Adaptive120s || self.resting.is_none() {
            let px = if self.policy == Policy::Adaptive120s {
                self.adaptive_price(d, at)
            } else {
                self.near(d)
            };
            if self
                .resting
                .is_none_or(|r| (r.price - px).abs() >= self.rule.tick_size * 0.5)
            {
                if self.resting.is_some() {
                    self.schedule(at + self.hop(), Action::Cancel);
                }
                self.schedule(
                    at + 2 * self.hop(),
                    Action::Place {
                        price: Some(px),
                        post_only: true,
                    },
                );
            }
        }
    }

    pub fn book(&mut self, at: u64, book: Option<&Depth>) {
        if let Some(d) = Self::fresh(book, at) {
            let mid = (d.bids[0].px + d.asks[0].px) / 2.0;
            let sign = if self.side == Side::Buy { 1.0 } else { -1.0 };
            for fill in &mut self.fills {
                for (index, ms) in [1_000, 15_000, 60_000, 300_000].into_iter().enumerate() {
                    let due = fill.at_ns + ms * NS_MS;
                    if at >= due
                        && at - due <= 2_000 * NS_MS
                        && fill.signed_markouts_bp[index].is_none()
                    {
                        fill.signed_markouts_bp[index] =
                            Some(sign * (mid - fill.price) / fill.price * 1e4);
                    }
                }
            }
        }
        if at >= self.mark_at_ns && self.mark.is_none() && at - self.mark_at_ns <= 2_000 * NS_MS {
            if let Some(d) = Self::fresh(book, at) {
                self.mark = Some((at, (d.bids[0].px + d.asks[0].px) / 2.0));
            }
        }
        if at >= self.mark_at_ns && self.started && !self.finished {
            self.incomplete = Some("execution_unfinished_at_common_horizon".into());
            self.finished = true;
            self.resting = None;
            self.pending.clear();
        }
        if !self.started || self.finished {
            return;
        }
        let Some(d) = book else {
            self.incomplete = Some("book_sequence_gap".into());
            self.finished = true;
            self.resting = None;
            self.pending.clear();
            return;
        };
        if let Some(mut resting) = self.resting {
            let displayed = self.displayed(d, resting.price);
            if self.queue_model == QueueModel::CancellationsAhead {
                let cancelled =
                    (resting.displayed - displayed - resting.trades_since_book).max(0.0);
                resting.queue = (resting.queue - cancelled).max(0.0);
            }
            resting.displayed = displayed;
            resting.trades_since_book = 0.0;
            self.resting = Some(resting);
        }
    }

    pub fn trade(
        &mut self,
        at: u64,
        price: f64,
        qty: f64,
        buyer_aggressor: bool,
        book: Option<&Depth>,
    ) {
        if self.started && !self.finished {
            self.trade_rows += 1;
        }
        if let Some(d) = book {
            let depth = if buyer_aggressor {
                d.asks[0].qty
            } else {
                d.bids[0].qty
            };
            let decay =
                (-((at.saturating_sub(self.flow_ns)) as f64) / (3_000.0 * NS_MS as f64)).exp();
            self.signed_flow = (self.signed_flow * decay
                + if buyer_aggressor { 1.0 } else { -1.0 } * qty / depth.max(qty))
            .clamp(-4.0, 4.0);
            self.flow_ns = at;
        }
        if !self.started || self.finished || (self.side == Side::Buy) == buyer_aggressor {
            return;
        }
        let Some(mut resting) = self.resting else {
            return;
        };
        let through = if self.side == Side::Buy {
            price < resting.price - self.rule.tick_size * 0.25
        } else {
            price > resting.price + self.rule.tick_size * 0.25
        };
        let equal = (price - resting.price).abs() < self.rule.tick_size * 0.25;
        if !through && !equal {
            return;
        }
        if through {
            resting.queue = 0.0;
        }
        let ahead = qty.min(resting.queue);
        resting.queue -= ahead;
        resting.trades_since_book += qty;
        self.resting = Some(resting);
        self.add_fill(at, qty - ahead, resting.price, true);
    }

    pub fn outcome(self) -> Outcome {
        let sign = if self.side == Side::Buy { 1.0 } else { -1.0 };
        let notional = self.qty * self.anchor;
        let filled = self.fills.iter().map(|f| f.qty).sum::<f64>();
        let price_cost = self
            .fills
            .iter()
            .map(|f| sign * f.qty * (f.price - self.anchor))
            .sum::<f64>()
            / notional
            * 1e4;
        let fees = self.fills.iter().map(|f| f.fee).sum::<f64>() / notional * 1e4;
        let missed = self.mark.map(|(_, mid)| {
            sign * (self.qty - filled).max(0.0) * (mid - self.anchor) / notional * 1e4
        });
        let needs_trades = self.policy != Policy::Cross
            && !(self.policy.native_work()
                && self.work.is_none()
                && matches!(self.original, OrderKind::Market));
        let incomplete = self
            .incomplete
            .or_else(|| {
                (needs_trades && self.trade_rows == 0)
                    .then(|| "no_public_trades_observed_during_attempt".into())
            })
            .or_else(|| {
                self.mark
                    .is_none()
                    .then(|| "missing_common_horizon_mark".into())
            });
        let total = if incomplete.is_none() {
            missed.map(|m| price_cost + fees + m)
        } else {
            None
        };
        Outcome {
            policy: self.policy,
            queue_model: self.queue_model,
            hop_ms: self.hop_ms,
            qty: self.qty,
            filled_qty: filled,
            maker_qty: self.fills.iter().filter(|f| f.maker).map(|f| f.qty).sum(),
            requested_notional: notional,
            fill_shortfall_bp: price_cost,
            fee_bp: fees,
            missed_opportunity_bp: missed,
            total_shortfall_bp: total,
            mark_ns: self.mark.map(|(at, _)| at),
            requests: self.requests,
            post_only_rejections: self.rejects,
            public_trade_rows: self.trade_rows,
            incomplete,
            fills: self.fills,
            decision_features: self.decision_features,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn order() -> ObservedOrder {
        ObservedOrder {
            request:serde_json::from_value(serde_json::json!({"client_order_id":"a","strategy":0,"symbol":0,"side":"Buy","qty":2.0,"kind":"Market","stop":null,"reduce_only":false})).unwrap(),
            intent:None,symbol:"XUSDT".into(),sleeve:"long".into(),engine_commit:None,source_segment:1,source_offset:8,
            process_epoch_ms:0,wire_mono_ns:0,decision_ns:Some(1_000_000_000),socket_write_ns:Some(1_000_000_000),transport_rtt_ns:None,
            arrival_mid:100.0,rule:Some(InstrumentRule{tick_size:1.0,qty_step:1.0,min_qty:1.0,min_notional:1.0}),
            fills:Default::default(),unidentified_fill_rows:0,terminal:None,amends:0,cancels:0,
        }
    }
    fn depth(at: u64, bid: f64, ask: f64, qty: f64) -> Depth {
        let mut d = Depth {
            recv_ns: at,
            bid_len: 1,
            ask_len: 1,
            ..Depth::default()
        };
        d.bids[0] = engine_types::BookLevel { px: bid, qty };
        d.asks[0] = engine_types::BookLevel { px: ask, qty };
        d
    }
    fn trial(queue: QueueModel) -> Trial {
        Trial::new(
            &order(),
            Policy::PostOnly120s,
            queue,
            100,
            Fees {
                maker: 0.00036,
                taker: 0.001,
                observed_ns: 0,
            },
        )
        .unwrap()
    }
    fn started(queue: QueueModel) -> Trial {
        let mut t = trial(queue);
        let d = depth(999_000_000, 99.0, 101.0, 10.0);
        t.advance(1_000_000_000, Some(&d));
        t.advance(1_100_000_000, Some(&d));
        t
    }

    #[test]
    fn queue_and_our_size_both_consume_finite_volume() {
        let mut t = started(QueueModel::TradesOnly);
        t.trade(1_200_000_000, 99.0, 11.0, false, None);
        assert_eq!(t.remaining(), 1.0);
        t.trade(1_300_000_000, 99.0, 0.5, false, None);
        assert_eq!(t.remaining(), 0.5);
        assert_eq!(t.fills[0].price, 99.0);
        assert!((t.fills[0].fee - 99.0 * 0.00036).abs() < 1e-12);
    }

    #[test]
    fn book_touch_never_fills_and_queue_cancellation_assumptions_stay_separate() {
        let mut conservative = started(QueueModel::TradesOnly);
        let mut optimistic = started(QueueModel::CancellationsAhead);
        let d = depth(1_200_000_000, 99.0, 101.0, 2.0);
        for t in [&mut conservative, &mut optimistic] {
            t.book(d.recv_ns, Some(&d));
            t.trade(1_300_000_000, 99.0, 3.0, false, Some(&d));
        }
        assert_eq!(conservative.remaining(), 2.0);
        assert_eq!(optimistic.remaining(), 1.0);
        let through_book = depth(1_400_000_000, 97.0, 98.0, 20.0);
        conservative.book(through_book.recv_ns, Some(&through_book));
        assert_eq!(conservative.remaining(), 2.0);
    }

    #[test]
    fn trade_through_still_has_finite_size_and_needs_the_correct_aggressor() {
        let mut t = started(QueueModel::TradesOnly);
        t.trade(1_200_000_000, 98.0, 100.0, true, None);
        assert_eq!(t.remaining(), 2.0);
        t.trade(1_300_000_000, 98.0, 0.5, false, None);
        assert_eq!(t.remaining(), 1.5);
    }

    #[test]
    fn trades_before_order_arrival_cannot_fill_and_post_only_rejects_after_price_moves() {
        let mut t = trial(QueueModel::TradesOnly);
        let old = depth(999_000_000, 99.0, 101.0, 10.0);
        t.advance(1_000_000_000, Some(&old));
        t.trade(1_050_000_000, 98.0, 100.0, false, Some(&old));
        assert_eq!(t.remaining(), 2.0);
        let moved = depth(1_080_000_000, 97.0, 98.0, 10.0);
        t.advance(1_100_000_000, Some(&moved));
        assert_eq!(t.rejects, 1);
        assert_eq!(t.remaining(), 2.0);
        assert!(t.resting.is_none());
    }

    #[test]
    fn adverse_fills_and_missed_winners_are_both_charged_at_the_same_horizon() {
        let mut missed = started(QueueModel::TradesOnly);
        missed.policy = Policy::PassiveSkip120s;
        missed.trade(1_200_000_000, 101.0, 1.0, true, None);
        let cancel_book = depth(121_000_000_000, 109.0, 111.0, 10.0);
        missed.advance(121_000_000_000, Some(&cancel_book));
        missed.advance(121_100_000_000, Some(&cancel_book));
        let mark = depth(missed.mark_at_ns, 109.0, 111.0, 10.0);
        missed.book(mark.recv_ns, Some(&mark));
        let outcome = missed.outcome();
        assert_eq!(outcome.total_shortfall_bp, Some(1_000.0));
        let mut filled = started(QueueModel::TradesOnly);
        filled.trade(1_200_000_000, 99.0, 12.0, false, None);
        let mark = depth(filled.mark_at_ns, 109.0, 111.0, 10.0);
        filled.book(mark.recv_ns, Some(&mark));
        let outcome = filled.outcome();
        assert!((outcome.total_shortfall_bp.unwrap() - (-100.0 + 3.564)).abs() < 1e-10);
    }

    #[test]
    fn stale_or_gapped_data_stays_unscored() {
        let mut t = started(QueueModel::TradesOnly);
        t.book(1_200_000_000, None);
        let mark = depth(t.mark_at_ns, 99.0, 101.0, 10.0);
        t.book(mark.recv_ns, Some(&mark));
        let outcome = t.outcome();
        assert_eq!(outcome.incomplete.as_deref(), Some("book_sequence_gap"));
        assert!(outcome.total_shortfall_bp.is_none());
        let mut t = trial(QueueModel::TradesOnly);
        let future = depth(1_050_000_000, 99.0, 101.0, 10.0);
        t.advance(1_000_000_000, Some(&future));
        assert_eq!(t.incomplete.as_deref(), Some("no_fresh_decision_book"));
    }

    #[test]
    fn deadline_cancel_race_fills_old_quote_then_crosses_only_the_remainder() {
        let mut t = started(QueueModel::TradesOnly);
        t.policy = Policy::PostOnly5s;
        let d = depth(6_000_000_000, 99.0, 101.0, 10.0);
        t.advance(d.recv_ns, Some(&d));
        t.trade(6_050_000_000, 99.0, 11.0, false, Some(&d));
        assert_eq!(t.remaining(), 1.0);
        t.advance(6_100_000_000, Some(&d));
        t.trade(6_150_000_000, 99.0, 100.0, false, Some(&d));
        assert_eq!(t.remaining(), 1.0);
        t.advance(6_200_000_000, Some(&d));
        assert_eq!(t.remaining(), 0.0);
        assert_eq!(t.fills.len(), 2);
        assert!(t.fills[0].maker);
        assert!(!t.fills[1].maker);
        assert_eq!(t.fills[1].qty, 1.0);
    }

    #[test]
    fn current_worked_limit_uses_the_native_deadline_cross() {
        let mut o = order();
        o.request.kind = OrderKind::Limit {
            px: 99.0,
            tif: TimeInForce::Gtc,
        };
        let policy = WorkPolicy {
            window_ms: 1_000,
            ..WorkPolicy::default()
        };
        o.intent=Some(serde_json::from_value(serde_json::json!({
            "strategy":0,"symbol":0,"side":"Buy","qty":2.0,"kind":"Market",
            "stop":null,"reduce_only":false,"tag":"test","decided_ns":1_000_000_000,"work":policy
        })).unwrap());
        let mut t = Trial::new(
            &o,
            Policy::Current,
            QueueModel::TradesOnly,
            100,
            Fees {
                maker: 0.00036,
                taker: 0.001,
                observed_ns: 0,
            },
        )
        .unwrap();
        let d = depth(999_000_000, 99.0, 101.0, 10.0);
        t.advance(t.start_ns, Some(&d));
        t.advance(t.start_ns + t.hop(), Some(&d));
        assert!(t.fills.is_empty());
        t.advance(2_100_000_000, Some(&d));
        t.advance(2_200_000_000, Some(&d));
        assert!(t.finished);
        assert_eq!(t.fills.len(), 1);
        assert!(!t.fills[0].maker);
        assert_eq!(t.fills[0].qty, 2.0);
    }

    #[test]
    fn sell_queue_and_costs_mirror_buy_and_replacement_loses_queue_priority() {
        let mut o = order();
        o.request.side = Side::Sell;
        let fees = Fees {
            maker: 0.00036,
            taker: 0.001,
            observed_ns: 0,
        };
        let mut t =
            Trial::new(&o, Policy::Adaptive120s, QueueModel::TradesOnly, 100, fees).unwrap();
        let d = depth(999_000_000, 99.0, 101.0, 10.0);
        t.advance(t.start_ns, Some(&d));
        t.advance(t.start_ns + t.hop(), Some(&d));
        t.trade(1_200_000_000, 101.0, 9.0, true, Some(&d));
        assert_eq!(t.resting.unwrap().queue, 1.0);
        let moved = depth(6_000_000_000, 98.0, 100.0, 10.0);
        t.advance(moved.recv_ns, Some(&moved));
        t.advance(6_100_000_000, Some(&moved));
        t.advance(6_200_000_000, Some(&moved));
        assert_eq!(t.resting.unwrap().queue, 10.0);
        t.trade(6_300_000_000, 100.0, 12.0, true, Some(&moved));
        let mark = depth(t.mark_at_ns, 89.0, 91.0, 10.0);
        t.book(mark.recv_ns, Some(&mark));
        let outcome = t.outcome();
        assert_eq!(outcome.filled_qty, 2.0);
        assert!((outcome.total_shortfall_bp.unwrap() - 3.6).abs() < 1e-9);
    }
    #[test]
    fn passive_entry_uses_native_deadline_and_crosses_only_the_unfilled_remainder() {
        let fees = Fees {
            maker: 0.00036,
            taker: 0.001,
            observed_ns: 0,
        };
        let mut t = Trial::new(
            &order(),
            Policy::PassiveEntry30s,
            QueueModel::TradesOnly,
            100,
            fees,
        )
        .unwrap();
        let d = depth(1_000_000_000, 99.0, 100.0, 10.0);
        t.advance(1_000_000_000, Some(&d));
        t.advance(1_100_000_000, Some(&d));
        assert_eq!(t.remaining(), 2.0);
        t.trade(1_200_000_000, 99.0, 11.0, false, Some(&d));
        assert_eq!(t.remaining(), 1.0);
        assert!(t.fills[0].maker);
        let d = depth(31_100_000_000, 99.0, 100.0, 10.0);
        t.advance(d.recv_ns, Some(&d));
        assert_eq!(t.remaining(), 1.0);
        t.advance(31_200_000_000, Some(&d));
        assert_eq!(t.remaining(), 0.0);
        assert!(!t.fills[1].maker);
        assert_eq!(t.fills.iter().map(|f| f.qty).sum::<f64>(), 2.0);
        assert_eq!(t.fills[1].price, 100.0);
        for (sleeve, reduce_only) in [("long", true), ("exodus", false), ("carry", false)] {
            let mut o = order();
            o.sleeve = sleeve.into();
            o.request.reduce_only = reduce_only;
            o.intent = Some(engine_types::Intent {
                exact_prices: None,
                exact_quantity: None,
                strategy: o.request.strategy,
                symbol: o.request.symbol,
                side: o.request.side,
                qty: o.request.qty,
                kind: OrderKind::Market,
                stop: None,
                reduce_only,
                tag: "scope".into(),
                decided_ns: 0,
                work: Some(WorkPolicy::default()),
                leverage: None,
            });
            let mut t = Trial::new(
                &o,
                Policy::PassiveEntry30s,
                QueueModel::TradesOnly,
                100,
                fees,
            )
            .unwrap();
            let d = depth(1_000_000_000, 98.0, 100.0, 10.0);
            t.advance(1_000_000_000, Some(&d));
            t.advance(1_100_000_000, Some(&d));
            assert_eq!(t.remaining(), 0.0);
            assert!(!t.fills[0].maker);
        }
    }
}
