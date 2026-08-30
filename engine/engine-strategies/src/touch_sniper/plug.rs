use engine_types::{
    Action, Feed, Intent, MarketEvent, OrderKind, OrderUpdate, Side, StopSpec, Strategy,
    StrategyCheckpoint, StrategyCtx, StrategyId, Subscription, SymbolId, TimerId,
};
use sha2::{Digest, Sha256};

use super::plan::{
    decide, DecisionInput, DecisionOutput, Effect, RestoreInput, RestoredCheckpoint, RestoredOrder,
    SniperCheckpoint, SniperConfig, SniperState, CHECKPOINT_SCHEMA_VERSION,
};
use crate::params::Params;
use crate::BuildError;

pub const NAME: &str = "touch_sniper";
const ENTRY_TAG: &str = "touch-entry";
const EXIT_TAG: &str = "touch-exit";

pub struct TouchSniper {
    id: StrategyId,
    config: SniperConfig,
    fingerprint: String,
    symbol: Option<SymbolId>,
    restored: bool,
    state: SniperState,
}

impl TouchSniper {
    pub fn from_params(id: StrategyId, params: &toml::Value) -> Result<Self, BuildError> {
        let p = Params::new(NAME, params)?;
        p.reject_unknown(&[
            "symbol",
            "side",
            "trigger_px",
            "qty",
            "stop_px",
            "take_px",
            "ttl_s",
        ])?;
        let symbol = p.string("symbol")?;
        if symbol.is_empty() {
            return Err(p.invalid("symbol", "expected a venue symbol, got an empty string"));
        }
        let side = match p.string("side")?.as_str() {
            "buy" => Side::Buy,
            "sell" => Side::Sell,
            other => {
                return Err(p.invalid(
                    "side",
                    format!("expected \"buy\" or \"sell\", got \"{other}\""),
                ))
            }
        };
        let ttl_ns = match p.opt_u64("ttl_s")? {
            None => None,
            Some(0) => {
                return Err(p.invalid("ttl_s", "expected a number of seconds above 0, got 0"))
            }
            Some(seconds) => Some(seconds.saturating_mul(1_000_000_000)),
        };
        let config = SniperConfig {
            symbol,
            side,
            trigger_px: p.positive("trigger_px")?,
            qty: p.positive("qty")?,
            stop_px: p.positive("stop_px")?,
            take_px: p.opt_positive("take_px")?,
            ttl_ns,
        };
        let canonical = serde_json::to_vec(&config)
            .map_err(|error| p.invalid("symbol", format!("cannot fingerprint config: {error}")))?;
        let fingerprint = Sha256::digest(canonical)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        Ok(Self {
            id,
            state: SniperState::armed(side),
            config,
            fingerprint,
            symbol: None,
            restored: false,
        })
    }

    fn resolve(&mut self, ctx: &dyn StrategyCtx) -> Option<SymbolId> {
        if self.symbol.is_none() {
            self.symbol = ctx.symbol_id(&self.config.symbol);
        }
        self.symbol
    }

    fn restore(&mut self, symbol: SymbolId, ctx: &mut dyn StrategyCtx) -> bool {
        if self.restored {
            return false;
        }
        let checkpoint = match ctx.strategy_checkpoint(symbol) {
            None => RestoredCheckpoint::Missing,
            Some(saved) if saved.decision_fingerprint != self.fingerprint => {
                RestoredCheckpoint::FingerprintMismatch
            }
            Some(saved) if saved.schema_version != CHECKPOINT_SCHEMA_VERSION => {
                RestoredCheckpoint::Invalid
            }
            Some(saved) => match serde_json::from_slice::<SniperCheckpoint>(&saved.payload) {
                Ok(checkpoint) if checkpoint.consumed => RestoredCheckpoint::Valid(checkpoint),
                Ok(_) | Err(_) => RestoredCheckpoint::Invalid,
            },
        };
        let mut orders = Vec::new();
        ctx.resting(&mut orders);
        let resting = orders
            .iter()
            .filter(|order| order.symbol == symbol)
            .map(|order| RestoredOrder {
                client_order_id: order.client_order_id.to_string(),
                side: order.side,
                qty: order.qty,
                filled_qty: order.filled_qty,
                reduce_only: order.reduce_only,
            })
            .collect();
        let output = decide(
            DecisionInput::Restore(RestoreInput {
                checkpoint,
                attributed_position: ctx.my_position(symbol),
                resting,
                wall_ms: ctx.wall_ms(),
            }),
            &self.state,
            &self.config,
        );
        self.restored = true;
        self.apply(symbol, output, ctx);
        true
    }

    fn apply(&mut self, symbol: SymbolId, output: DecisionOutput, ctx: &mut dyn StrategyCtx) {
        self.state = output.state;
        for effect in output.effects {
            match effect {
                Effect::Save(checkpoint) => self.save(symbol, checkpoint, ctx),
                Effect::PlaceEntry => ctx.place(Intent {
                    strategy: self.id,
                    symbol,
                    side: self.config.side,
                    qty: self.config.qty,
                    kind: OrderKind::Market,
                    stop: Some(StopSpec {
                        trigger_px: self.config.stop_px,
                    }),
                    reduce_only: false,
                    tag: ENTRY_TAG.to_string(),
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                }),
                Effect::CancelEntry { client_order_id } => ctx.cancel(symbol, &client_order_id),
                Effect::ArmCancelRetry => {
                    ctx.arm_timer(super::plan::CANCEL_RETRY_TIMER, 1_000_000_000)
                }
                Effect::PlaceExit { side, qty } => ctx.place(Intent {
                    strategy: self.id,
                    symbol,
                    side,
                    qty,
                    kind: OrderKind::Market,
                    stop: None,
                    reduce_only: true,
                    tag: EXIT_TAG.to_string(),
                    decided_ns: ctx.now_ns(),
                    work: None,
                    leverage: None,
                }),
                Effect::ArmTtl { after_ns } => ctx.arm_timer(super::plan::TTL_TIMER, after_ns),
            }
        }
    }

    fn save(&self, symbol: SymbolId, state: SniperCheckpoint, ctx: &mut dyn StrategyCtx) {
        let payload = serde_json::to_vec(&state).expect("sniper checkpoint has a fixed schema");
        ctx.emit(Action::SetStrategyCheckpoint {
            strategy: self.id,
            symbol,
            checkpoint: StrategyCheckpoint {
                schema_version: CHECKPOINT_SCHEMA_VERSION,
                decision_fingerprint: self.fingerprint.clone(),
                payload,
            },
        });
    }

    fn decide_after_restore(
        &mut self,
        symbol: SymbolId,
        input: DecisionInput<'_>,
        ctx: &mut dyn StrategyCtx,
    ) {
        let restored_now = self.restore(symbol, ctx);
        // The engine updates attribution and its resting-order ledger before
        // routing order news. Restore already contains that order's result.
        if restored_now && matches!(input, DecisionInput::Order { .. }) {
            return;
        }
        let output = decide(input, &self.state, &self.config);
        self.apply(symbol, output, ctx);
    }
}

impl Strategy for TouchSniper {
    fn name(&self) -> &str {
        NAME
    }

    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription {
            symbol: self.config.symbol.clone(),
            feed: Feed::Quote,
        }]
    }

    fn on_market(&mut self, event: &MarketEvent, ctx: &mut dyn StrategyCtx) {
        let Some(symbol) = self.resolve(&*ctx) else {
            return;
        };
        if let MarketEvent::Quote {
            symbol: from,
            quote,
        } = event
        {
            if *from == symbol {
                self.decide_after_restore(
                    symbol,
                    DecisionInput::Quote {
                        bid_px: quote.bid_px,
                        ask_px: quote.ask_px,
                        wall_ms: ctx.wall_ms(),
                    },
                    ctx,
                );
            }
        }
    }

    fn on_timer(&mut self, id: TimerId, _now_ns: u64, ctx: &mut dyn StrategyCtx) {
        let Some(symbol) = self.resolve(&*ctx) else {
            return;
        };
        self.decide_after_restore(symbol, DecisionInput::Timer { id }, ctx);
    }

    fn on_order(&mut self, update: &OrderUpdate, ctx: &mut dyn StrategyCtx) {
        let Some(symbol) = self.resolve(&*ctx) else {
            return;
        };
        self.decide_after_restore(
            symbol,
            DecisionInput::Order {
                update,
                wall_ms: ctx.wall_ms(),
            },
            ctx,
        );
    }

    fn on_intent_refused(
        &mut self,
        symbol: SymbolId,
        reduce_only: bool,
        _reason: &str,
        ctx: &mut dyn StrategyCtx,
    ) {
        if self.resolve(&*ctx) != Some(symbol) {
            return;
        }
        self.decide_after_restore(symbol, DecisionInput::IntentRefused { reduce_only }, ctx);
    }
}
