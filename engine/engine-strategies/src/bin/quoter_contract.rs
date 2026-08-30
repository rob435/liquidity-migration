//! Line-delimited replay adapter for the quoter's Rust decision contract.

use std::collections::BTreeMap;
use std::io::{self, BufRead, Write};

use engine_strategies::quoter::plan::{
    decide, DecisionInput, DecisionRules, MicroRules, MicroState, QuoteEffect, QuoteRules,
    SignalInput, WorkingQuote,
};
use engine_types::{BookLevel, Depth, InstrumentRule, Quote, Side, TradeFlow, BOOK_DEPTH};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize)]
struct Init {
    half_spread_bps: f64,
    requote_bps: f64,
    signal_half_life_ms: f64,
    flow_fast_half_life_ms: f64,
    flow_slow_half_life_ms: f64,
    flow_fast_weight: f64,
    flow_slow_weight: f64,
    flow_depth_bps: f64,
    flow_volatility_depth_multiplier: f64,
    flow_max_score: f64,
    arms: Vec<Arm>,
}

#[derive(Clone, Debug, Deserialize)]
struct Arm {
    name: String,
    maker_fee_bps: f64,
    min_edge_bps: f64,
    volatility_multiplier: f64,
    toxicity_bps: f64,
    book_lean_bps: f64,
    trade_lean_bps: f64,
    flow_response_bps: f64,
    flow_max_widen_bps: f64,
    flow_pull_score: Option<f64>,
    queue_reprice_edge_bps: f64,
}

impl Init {
    fn base(&self) -> QuoteRules {
        QuoteRules {
            half_spread: self.half_spread_bps / 10_000.0,
            requote_tolerance: self.requote_bps / 10_000.0,
            qty: 1.0,
            max_position: 1_000_000_000.0,
            skew: 0.0,
            stop_loss_fraction: 0.5,
        }
    }

    fn micro(&self, arm: &Arm) -> MicroRules {
        MicroRules {
            maker_fee: arm.maker_fee_bps / 10_000.0,
            min_edge: arm.min_edge_bps / 10_000.0,
            volatility_multiplier: arm.volatility_multiplier,
            toxicity: arm.toxicity_bps / 10_000.0,
            book_lean: arm.book_lean_bps / 10_000.0,
            trade_lean: arm.trade_lean_bps / 10_000.0,
            signal_half_life_ns: millis_to_ns(self.signal_half_life_ms),
            flow_fast_half_life_ns: millis_to_ns(self.flow_fast_half_life_ms),
            flow_slow_half_life_ns: millis_to_ns(self.flow_slow_half_life_ms),
            flow_fast_weight: self.flow_fast_weight,
            flow_slow_weight: self.flow_slow_weight,
            flow_response: arm.flow_response_bps / 10_000.0,
            flow_max_widen: arm.flow_max_widen_bps / 10_000.0,
            flow_pull_score: arm.flow_pull_score,
            flow_depth_bps: self.flow_depth_bps,
            flow_volatility_depth_multiplier: self.flow_volatility_depth_multiplier,
            flow_max_score: self.flow_max_score,
            queue_reprice_edge: arm.queue_reprice_edge_bps / 10_000.0,
            qty_usdt: None,
            max_position_usdt: None,
            adaptive: true,
        }
    }

    fn rules(&self, arm: &Arm) -> DecisionRules {
        DecisionRules {
            quote: self.base(),
            micro: self.micro(arm),
            quote_enabled: true,
        }
    }
}

fn millis_to_ns(value: f64) -> u64 {
    (value * 1_000_000.0).round() as u64
}

#[derive(Clone, Debug, Deserialize)]
struct Request {
    tick_size: f64,
    event: Event,
    working: BTreeMap<String, Working>,
}

#[derive(Clone, Debug, Default, Deserialize)]
struct Working {
    bid: Option<f64>,
    ask: Option<f64>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
enum Event {
    Depth {
        recv_ns: u64,
        bids: Vec<[f64; 2]>,
        asks: Vec<[f64; 2]>,
    },
    Trades {
        recv_ns: u64,
        buy_qty: f64,
        sell_qty: f64,
        last_px: f64,
    },
    Touch {
        recv_ns: u64,
        bid: [f64; 2],
        ask: [f64; 2],
    },
}

#[derive(Clone, Debug, Serialize)]
struct Response {
    prices: BTreeMap<String, Prices>,
}

#[derive(Copy, Clone, Debug, Serialize)]
struct Prices {
    bid: Option<f64>,
    ask: Option<f64>,
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut lines = stdin.lock().lines();
    let first = lines.next().ok_or("missing init line")??;
    let init: Init = serde_json::from_str(&first)?;
    if init.arms.is_empty() {
        return Err("init needs at least one arm".into());
    }
    let mut states = BTreeMap::<String, MicroState>::new();
    let mut depth = Depth::default();
    let mut quote = Quote::default();
    let stdout = io::stdout();
    let mut out = io::BufWriter::new(stdout.lock());

    for line in lines {
        let request: Request = serde_json::from_str(&line?)?;
        let signal = match request.event {
            Event::Depth {
                recv_ns,
                bids,
                asks,
            } => {
                depth = wire_depth(recv_ns, &bids, &asks)?;
                quote = depth.quote();
                SignalInput::Depth {
                    bids: &depth.bids[..depth.bid_len as usize],
                    asks: &depth.asks[..depth.ask_len as usize],
                    recv_ns,
                }
            }
            Event::Trades {
                recv_ns,
                buy_qty,
                sell_qty,
                last_px,
            } => SignalInput::Trades(TradeFlow {
                buy_qty,
                sell_qty,
                last_px,
                recv_ns,
                ..TradeFlow::default()
            }),
            Event::Touch { recv_ns, bid, ask } => {
                quote = Quote {
                    bid_px: bid[0],
                    bid_qty: bid[1],
                    ask_px: ask[0],
                    ask_qty: ask[1],
                    recv_ns,
                    ..Quote::default()
                };
                SignalInput::Touch {
                    bid: BookLevel {
                        px: bid[0],
                        qty: bid[1],
                    },
                    ask: BookLevel {
                        px: ask[0],
                        qty: ask[1],
                    },
                    recv_ns,
                }
            }
        };

        let prices = init
            .arms
            .iter()
            .map(|arm| {
                let held = request.working.get(&arm.name).cloned().unwrap_or_default();
                let working = working_quotes(&held);
                let output = decide(
                    states.get(&arm.name).copied().unwrap_or_default(),
                    DecisionInput {
                        signal: Some(signal),
                        quote,
                        depth: &depth,
                        position: 0.0,
                        working: &working,
                        foreign_owner: false,
                        flatten_pending: false,
                        instrument: Some(InstrumentRule {
                            tick_size: request.tick_size,
                            qty_step: 0.0,
                            min_qty: 0.0,
                            min_notional: 0.0,
                        }),
                    },
                    init.rules(arm),
                );
                states.insert(arm.name.clone(), output.state);
                let next = apply_effects(
                    held,
                    &output.effects,
                    quote.bid_px,
                    quote.ask_px,
                    request.tick_size,
                );
                (arm.name.clone(), next)
            })
            .collect();
        serde_json::to_writer(&mut out, &Response { prices })?;
        out.write_all(b"\n")?;
        out.flush()?;
    }
    Ok(())
}

fn wire_depth(recv_ns: u64, bids: &[[f64; 2]], asks: &[[f64; 2]]) -> Result<Depth, &'static str> {
    if bids.len() > BOOK_DEPTH || asks.len() > BOOK_DEPTH {
        return Err("depth exceeds the engine's L50 contract");
    }
    let mut depth = Depth {
        bid_len: bids.len() as u8,
        ask_len: asks.len() as u8,
        recv_ns,
        ..Depth::default()
    };
    for (slot, level) in depth.bids.iter_mut().zip(bids) {
        *slot = BookLevel {
            px: level[0],
            qty: level[1],
        };
    }
    for (slot, level) in depth.asks.iter_mut().zip(asks) {
        *slot = BookLevel {
            px: level[0],
            qty: level[1],
        };
    }
    Ok(depth)
}

fn working_quotes(working: &Working) -> Vec<WorkingQuote> {
    [
        (Side::Buy, "bid", working.bid),
        (Side::Sell, "ask", working.ask),
    ]
    .into_iter()
    .filter_map(|(side, id, px)| {
        px.map(|px| WorkingQuote {
            client_order_id: id.to_string(),
            side,
            px,
            remaining_qty: 1.0,
            reduce_only: false,
        })
    })
    .collect()
}

fn apply_effects(
    working: Working,
    effects: &[QuoteEffect],
    bid: f64,
    ask: f64,
    tick: f64,
) -> Prices {
    let mut next = Prices {
        bid: working.bid,
        ask: working.ask,
    };
    for effect in effects {
        match effect {
            QuoteEffect::Place { side, px, .. } => set_side(
                &mut next,
                *side,
                Some(executable_quote_px(*side, *px, bid, ask, tick)),
            ),
            QuoteEffect::Move {
                client_order_id,
                px,
            } => {
                let side = if client_order_id == "ask" {
                    Side::Sell
                } else {
                    Side::Buy
                };
                set_side(
                    &mut next,
                    side,
                    Some(executable_quote_px(side, *px, bid, ask, tick)),
                );
            }
            QuoteEffect::Pull { client_order_id } => {
                let side = if client_order_id == "ask" {
                    Side::Sell
                } else {
                    Side::Buy
                };
                set_side(&mut next, side, None);
            }
            QuoteEffect::Flatten { .. } => {}
        }
    }
    next
}

fn set_side(prices: &mut Prices, side: Side, px: Option<f64>) {
    match side {
        Side::Buy => prices.bid = px,
        Side::Sell => prices.ask = px,
    }
}

/// Apply the engine's directional tick rounding to a reducer target.
fn executable_quote_px(side: Side, wanted: f64, bid: f64, ask: f64, tick: f64) -> f64 {
    let passive = match side {
        Side::Buy => wanted.min(ask - tick),
        Side::Sell => wanted.max(bid + tick),
    };
    if !passive.is_finite() || !tick.is_finite() || tick <= 0.0 {
        return passive;
    }
    engine_types::quantize::quantize_px(
        passive,
        side,
        &InstrumentRule {
            tick_size: tick,
            qty_step: 0.0,
            min_qty: 0.0,
            min_notional: 0.0,
        },
    )
}
