//! Persistent JSONL adapter for native reducer research replay.

use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, BufRead, Write};

use engine_strategies::native_carry::plan::{
    reduce_lifecycle as reduce_carry, reduce_signal as reduce_carry_signal, CarrySignalBatch,
    ReducerInput as CarryInput, SleeveState as CarryState, StrategyConfig as CarryConfig,
};
use engine_strategies::native_carry::scorer::CarryDecision;
use engine_strategies::native_carry::scorer::{
    score_research as score_carry_research, CarryFeatureRow, ResearchRuleConfig,
};
use engine_strategies::native_common::{CarryPresettlementFire, PlannerFacts};
use engine_strategies::native_exodus::plan::{
    reduce as reduce_exodus, ReducerInput as ExodusInput, SleeveState as ExodusState,
    StrategyConfig as ExodusConfig,
};
use engine_strategies::native_long::plan::{
    classify_feature as classify_long, decide as decide_long, reduce_batch as reduce_long,
    BatchInput as LongInput, DecisionInput as LongDecisionInput, FeatureRow as LongFeatureRow,
    PriorState as LongPriorState, RuleConfig as LongRuleConfig, SleeveState as LongState,
    StrategyConfig as LongConfig,
};
use engine_strategies::position_plan::Held;
use engine_types::InstrumentRule;
use serde::Deserialize;
use serde_json::{json, Value};

#[derive(Deserialize)]
#[serde(tag = "operation", rename_all = "snake_case", deny_unknown_fields)]
enum Request {
    LongClassify {
        schema_version: u16,
        config: LongRuleConfig,
        rows: Vec<LongFeatureRow>,
    },
    LongDecide {
        schema_version: u16,
        config: LongConfig,
        input: LongDecisionInput,
        prior: LongPriorState,
    },
    LongReduce {
        schema_version: u16,
        config: LongConfig,
        input: LongInputWire,
        prior: LongState,
    },
    CarryReduce {
        schema_version: u16,
        config: CarryConfig,
        input: Box<CarryInputWire>,
        prior: CarryState,
        signal_batch: Option<CarrySignalBatch>,
    },
    CarryResearchScore {
        schema_version: u16,
        config: ResearchRuleConfig,
        rows: Vec<CarryFeatureRow>,
    },
    ExodusReduce {
        schema_version: u16,
        config: ExodusConfig,
        input: ExodusInputWire,
        prior: ExodusState,
    },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FactsWire {
    held: BTreeMap<String, Held>,
    prices: BTreeMap<String, f64>,
    rules: BTreeMap<String, InstrumentRule>,
}

impl FactsWire {
    fn into_facts(self) -> PlannerFacts {
        PlannerFacts {
            held: self.held,
            prices: self.prices,
            rules: self.rules,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LongInputWire {
    decisions: Vec<LongDecisionInput>,
    facts: FactsWire,
    owned_working_symbols: BTreeSet<String>,
    owned_opening_order_ids: BTreeMap<String, Vec<String>>,
    checkpoint_fingerprint: Option<String>,
    signal_receipt: Option<(String, u64, String)>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CarryInputWire {
    now_ms: i64,
    decision: CarryDecision,
    upcoming_decision: Option<CarryDecision>,
    settled_funding: Vec<engine_strategies::native_carry::plan::SettledFundingObservation>,
    presettlement: Vec<engine_strategies::native_carry::plan::PresettlementObservation>,
    durable_fires: Vec<CarryPresettlementFire>,
    trail_by_symbol: BTreeMap<String, f64>,
    entry_blockers: BTreeMap<String, String>,
    account_healthy: bool,
    equity_usdt: f64,
    upcoming_sizing_equity_usdt: Option<f64>,
    facts: FactsWire,
    owned_working_symbols: BTreeSet<String>,
    owned_opening_order_ids: BTreeMap<String, Vec<String>>,
    checkpoint_fingerprint: Option<String>,
    signal_receipt: Option<(String, u64, String)>,
}

impl CarryInputWire {
    fn into_input(self) -> CarryInput {
        CarryInput {
            now_ms: self.now_ms,
            decision: self.decision,
            upcoming_decision: self.upcoming_decision,
            settled_funding: self.settled_funding,
            presettlement: self.presettlement,
            durable_fires: self.durable_fires,
            trail_by_symbol: self.trail_by_symbol,
            entry_blockers: self.entry_blockers,
            account_healthy: self.account_healthy,
            equity_usdt: self.equity_usdt,
            upcoming_sizing_equity_usdt: self.upcoming_sizing_equity_usdt,
            facts: self.facts.into_facts(),
            owned_working_symbols: self.owned_working_symbols,
            owned_opening_order_ids: self.owned_opening_order_ids,
            checkpoint_fingerprint: self.checkpoint_fingerprint,
            signal_receipt: self.signal_receipt,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ExodusInputWire {
    now_ms: i64,
    events: Vec<CarryPresettlementFire>,
    facts: FactsWire,
    owned_working_symbols: BTreeSet<String>,
    owned_opening_order_ids: BTreeMap<String, Vec<String>>,
    account_healthy: bool,
    checkpoint_fingerprint: Option<String>,
}

fn execute(request: Request) -> Result<Value, String> {
    match request {
        Request::LongClassify {
            schema_version,
            config,
            rows,
        } => {
            validate_schema(schema_version)?;
            config.validate_classification().map_err(str::to_owned)?;
            Ok(json!({
                "schema_version": 1,
                "classifications": rows
                    .iter()
                    .map(|row| classify_long(row, &config))
                    .collect::<Vec<_>>(),
            }))
        }
        Request::LongDecide {
            schema_version,
            config,
            input,
            prior,
        } => {
            validate_schema(schema_version)?;
            decide_long(&input, &prior, &config)
                .map(|output| json!(output))
                .map_err(str::to_owned)
        }
        Request::LongReduce {
            schema_version,
            config,
            input,
            prior,
        } => {
            validate_schema(schema_version)?;
            reduce_long(
                LongInput {
                    decisions: input.decisions,
                    facts: input.facts.into_facts(),
                    owned_working_symbols: input.owned_working_symbols,
                    owned_opening_order_ids: input.owned_opening_order_ids,
                    checkpoint_fingerprint: input.checkpoint_fingerprint,
                    signal_receipt: input.signal_receipt,
                },
                prior,
                &config,
            )
            .map(|output| json!(output))
            .map_err(str::to_owned)
        }
        Request::CarryReduce {
            schema_version,
            config,
            input,
            prior,
            signal_batch,
        } => {
            validate_schema(schema_version)?;
            let input = (*input).into_input();
            let output = match signal_batch {
                Some(batch) => reduce_carry_signal(batch, input, prior, &config),
                None => reduce_carry(input, prior, &config),
            }
            .map_err(str::to_owned)?;
            Ok(json!(output))
        }
        Request::CarryResearchScore {
            schema_version,
            config,
            rows,
        } => {
            validate_schema(schema_version)?;
            score_carry_research(&rows, &config)
                .map(|weights| json!({"schema_version": 1, "weights": weights}))
                .map_err(str::to_owned)
        }
        Request::ExodusReduce {
            schema_version,
            config,
            input,
            prior,
        } => {
            validate_schema(schema_version)?;
            reduce_exodus(
                ExodusInput {
                    now_ms: input.now_ms,
                    events: input.events,
                    facts: input.facts.into_facts(),
                    owned_working_symbols: input.owned_working_symbols,
                    owned_opening_order_ids: input.owned_opening_order_ids,
                    account_healthy: input.account_healthy,
                    checkpoint_fingerprint: input.checkpoint_fingerprint,
                },
                prior,
                &config,
            )
            .map(|output| json!(output))
            .map_err(str::to_owned)
        }
    }
}

fn validate_schema(schema_version: u16) -> Result<(), String> {
    if schema_version != 1 {
        return Err("unsupported strategy-contract request schema".to_owned());
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let stdin = io::stdin();
    let mut stdout = io::BufWriter::new(io::stdout().lock());
    for line in stdin.lock().lines() {
        let line = line?;
        let response = match serde_json::from_str::<Request>(&line) {
            Ok(request) => match execute(request) {
                Ok(output) => json!({"ok": true, "output": output}),
                Err(error) => json!({"ok": false, "error": error}),
            },
            Err(error) => json!({"ok": false, "error": format!("invalid request: {error}")}),
        };
        serde_json::to_writer(&mut stdout, &response)?;
        stdout.write_all(b"\n")?;
        stdout.flush()?;
    }
    Ok(())
}
