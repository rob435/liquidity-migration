use std::collections::BTreeMap;
use std::fmt::Write;

use serde_json::{json, Value};

use super::runner::OrderResult;
use super::sim::Policy;

#[derive(Default)]
struct Paired {
    orders: usize,
    weight: f64,
    saving: f64,
    price: f64,
    fee: f64,
    missed: f64,
    filled: f64,
    maker: f64,
    requests: u64,
}

pub fn metrics(results: &[OrderResult]) -> Value {
    let mut paired: BTreeMap<String, Paired> = BTreeMap::new();
    let mut failures: BTreeMap<String, usize> = BTreeMap::new();
    let mut actual: BTreeMap<String, Vec<&OrderResult>> = BTreeMap::new();
    let mut slices: BTreeMap<String, Vec<&OrderResult>> =
        BTreeMap::from([("all".into(), Vec::new())]);
    let mut calibration: BTreeMap<String, Vec<f64>> = BTreeMap::new();
    for result in results {
        let order = &result.observed;
        let action = if order.request.reduce_only {
            "reduce"
        } else {
            "open"
        };
        actual
            .entry(format!("{}|{}|{action}", order.sleeve, order.symbol))
            .or_default()
            .push(result);
        for slice in [
            "all".to_string(),
            format!("sleeve|{}", order.sleeve),
            format!("action|{action}"),
        ] {
            slices.entry(slice).or_default().push(result);
        }
        if let Some(at) = order.decision_ns {
            slices
                .entry(format!("day|{}", &timestamp(at)[..10]))
                .or_default()
                .push(result);
        }
        if let Some(reason) = &result.unavailable {
            *failures.entry(reason.clone()).or_default() += 1;
        }
        if order.unidentified_fill_rows == 0
            && matches!(order.request.kind, engine_types::OrderKind::Market)
        {
            let actual_qty: f64 = order.fills.values().map(|f| f.qty).sum();
            if (actual_qty - order.request.qty).abs() <= order.request.qty * 1e-8
                && actual_qty > 0.0
            {
                let actual_px =
                    order.fills.values().map(|f| f.qty * f.price).sum::<f64>() / actual_qty;
                let sign = if order.request.side == engine_types::Side::Buy {
                    1.0
                } else {
                    -1.0
                };
                for trial in &result.hypothetical {
                    if trial.policy == Policy::Cross
                        && trial.queue_model == super::sim::QueueModel::TradesOnly
                        && trial.incomplete.is_none()
                        && (trial.filled_qty - actual_qty).abs() <= actual_qty * 1e-8
                    {
                        let simulated = trial.fills.iter().map(|f| f.qty * f.price).sum::<f64>()
                            / trial.filled_qty;
                        calibration
                            .entry(format!("{}|{}ms", order.symbol, trial.hop_ms))
                            .or_default()
                            .push(sign * (simulated - actual_px) / actual_px * 1e4);
                    }
                }
            }
        }
        for other in &result.hypothetical {
            if let Some(reason) = &other.incomplete {
                *failures.entry(reason.clone()).or_default() += 1;
            }
            let baseline = result.hypothetical.iter().find(|c| {
                c.policy == Policy::Cross
                    && c.hop_ms == other.hop_ms
                    && c.queue_model == other.queue_model
            });
            let Some((cross, cost)) =
                baseline.and_then(|c| c.total_shortfall_bp.zip(other.total_shortfall_bp))
            else {
                continue;
            };
            for slice in [
                action.to_string(),
                format!("{}|{action}", order.sleeve),
                format!(
                    "{}|{action}",
                    &timestamp(order.decision_ns.unwrap_or_default())[..10]
                ),
            ] {
                let key = format!(
                    "{slice}|{}|{:?}|{}ms",
                    other.policy.name(),
                    other.queue_model,
                    other.hop_ms
                );
                let p = paired.entry(key).or_default();
                let w = other.requested_notional;
                p.orders += 1;
                p.weight += w;
                p.saving += (cross - cost) * w;
                p.price += other.fill_shortfall_bp * w;
                p.fee += other.fee_bp * w;
                p.missed += other.missed_opportunity_bp.unwrap_or_default() * w;
                p.filled += other.filled_qty / other.qty * w;
                p.maker += other.maker_qty / other.qty * w;
                p.requests += other.requests;
            }
        }
    }
    let paired:BTreeMap<_,_>=paired.into_iter().map(|(key,p)|(key,json!({
        "paired_orders":p.orders,"requested_notional_usdt":p.weight,
        "saving_vs_cross_bp":p.saving/p.weight,"price_cost_bp":p.price/p.weight,
        "fee_bp":p.fee/p.weight,"missed_opportunity_bp":p.missed/p.weight,
        "filled_fraction":p.filled/p.weight,"maker_fraction_of_requested":p.maker/p.weight,
        "requests_per_order":p.requests as f64/p.orders as f64
    }))).collect();
    let actual: BTreeMap<_, _> = actual
        .into_iter()
        .map(|(key, orders)| (key, actual_metrics(&orders)))
        .collect();
    let actual_by_slice: BTreeMap<_, _> = slices
        .into_iter()
        .map(|(key, orders)| (key, actual_metrics(&orders)))
        .collect();
    let actual_by_order: BTreeMap<_, _> = results
        .iter()
        .map(|result| {
            let order = &result.observed;
            let mut costs = actual_metrics(&[result]);
            costs["symbol"] = json!(order.symbol);
            costs["sleeve"] = json!(order.sleeve);
            costs["side"] = json!(order.request.side);
            costs["reduce_only"] = json!(order.request.reduce_only);
            costs["decision_ns"] = json!(order.decision_ns);
            (order.request.client_order_id.clone(), costs)
        })
        .collect();
    let calibration:BTreeMap<_,_>=calibration.into_iter().map(|(key,errors)|(key,json!({
        "orders":errors.len(),"mean_signed_price_error_bp":errors.iter().sum::<f64>()/errors.len() as f64,
        "mean_absolute_price_error_bp":errors.iter().map(|e|e.abs()).sum::<f64>()/errors.len() as f64,
        "max_absolute_price_error_bp":errors.iter().map(|e|e.abs()).fold(0.0,f64::max)
    }))).collect();
    json!({"actual":actual,"actual_by_slice":actual_by_slice,"actual_by_order":actual_by_order,"paired_by_slice":paired,"market_calibration":calibration,"unavailable_order_or_arm_counts":failures})
}

fn actual_metrics(orders: &[&OrderResult]) -> Value {
    let mut fills = 0;
    let mut notional = 0.0;
    let mut maker_notional = 0.0;
    let mut fee = 0.0;
    let mut fee_notional = 0.0;
    let mut unknown_fees = 0;
    let mut mark_sum = [0.0; 4];
    let mut mark_weight = [0.0; 4];
    let mut mark_counts = [0_u64; 4];
    let mut price_cost = 0.0;
    let mut price_weight = 0.0;
    let mut missing_mid = 0;
    let mut costed_fills = 0;
    let mut costed_reference = 0.0;
    let mut costed_notional = 0.0;
    let mut costed_price = 0.0;
    let mut costed_fee = 0.0;
    let mut rtts = Vec::new();
    let mut prewires = Vec::new();
    for result in orders {
        let o = &result.observed;
        rtts.extend(o.transport_rtt_ns);
        prewires.extend(
            o.socket_write_ns
                .zip(o.decision_ns)
                .and_then(|(w, d)| w.checked_sub(d)),
        );
        for (id, f) in &o.fills {
            fills += 1;
            let w = f.qty * f.price;
            notional += w;
            if f.maker {
                maker_notional += w;
            }
            if let Some(value) = f.fee {
                fee += value;
                fee_notional += w;
            } else {
                unknown_fees += 1;
            }
            if o.arrival_mid.is_finite() && o.arrival_mid > 0.0 {
                let sign = if o.request.side == engine_types::Side::Buy {
                    1.0
                } else {
                    -1.0
                };
                price_cost += sign * f.qty * (f.price - o.arrival_mid);
                price_weight += f.qty * o.arrival_mid;
                if let Some(fee) = f.fee {
                    costed_fills += 1;
                    costed_reference += f.qty * o.arrival_mid;
                    costed_notional += w;
                    costed_price += sign * f.qty * (f.price - o.arrival_mid);
                    costed_fee += fee;
                }
            } else {
                missing_mid += 1;
            }
            if let Some(marks) = result.actual_markouts_bp.get(id) {
                for i in 0..4 {
                    if let Some(mark) = marks[i] {
                        mark_sum[i] += mark * w;
                        mark_weight[i] += w;
                        mark_counts[i] += 1;
                    }
                }
            }
        }
    }
    rtts.sort_unstable();
    prewires.sort_unstable();
    let ratio = |a: f64, b: f64| if b > 0.0 { Some(a / b) } else { None };
    let percentile = |v: &[u64], p: usize| {
        v.get((v.len().saturating_sub(1)) * p / 100)
            .map(|n| *n as f64 / 1e6)
    };
    json!({"orders":orders.len(),"fills":fills,"filled_notional_usdt":notional,
        "unidentified_fill_rows":orders.iter().map(|o|o.observed.unidentified_fill_rows).sum::<u64>(),
        "maker_notional_fraction":ratio(maker_notional,notional),"known_fee_usdt":fee,
        "known_fee_bp":ratio(fee*1e4,fee_notional),"fills_without_fee":unknown_fees,
        "filled_price_shortfall_bp":ratio(price_cost*1e4,price_weight),
        "fills_without_arrival_mid":missing_mid,
        "costed":{
            "fills":costed_fills,"reference_notional_usdt":costed_reference,
            "filled_notional_usdt":costed_notional,"fill_notional_coverage":ratio(costed_notional,notional),
            "price_cost_usdt":(costed_fills>0).then_some(costed_price),
            "fee_usdt":(costed_fills>0).then_some(costed_fee),
            "total_cost_usdt":(costed_fills>0).then_some(costed_price+costed_fee),
            "price_shortfall_bp":ratio(costed_price*1e4,costed_reference),
            "fee_bp":ratio(costed_fee*1e4,costed_reference),
            "total_shortfall_bp":ratio((costed_price+costed_fee)*1e4,costed_reference)
        },
        "markout_horizons_s":[1,15,60,300],"signed_markouts_bp":(0..4).map(|i|ratio(mark_sum[i],mark_weight[i])).collect::<Vec<_>>(),"markout_fill_counts":mark_counts,
        "transport_rtt_p50_ms":percentile(&rtts,50),"transport_rtt_p90_ms":percentile(&rtts,90),
        "decision_to_socket_p50_ms":percentile(&prewires,50),"decision_to_socket_p90_ms":percentile(&prewires,90)
    })
}

pub fn text(report: &Value) -> Result<String, std::fmt::Error> {
    let mut out=format!("One-sided execution study\nGenerated: {} | commit: {}\nWindow starts: {} | aligned orders: {} | cached: {}\n\n",
        timestamp(report["generated_ns"].as_u64().unwrap_or_default()),report["code_commit"].as_str().unwrap_or("unknown"),timestamp(report["window_start_ns"].as_u64().unwrap_or_default()),report["orders"].as_array().map_or(0,Vec::len),report["cached_orders"]);
    out.push_str("MEASURED EXECUTION COSTS (identified WAL fills; not whole-account P&L)\nSlippage includes spread crossing and price movement from the recorded order-arrival midpoint.\nCost components use the same fills and arrival-notional denominator. Funding is separate.\nSlice | orders | fills | costed % | slippage bp | fee bp | all-in bp | cost USDT | maker %\n");
    if let Some(rows) = report["metrics"]["actual_by_slice"].as_object() {
        for (key, r) in rows {
            cost_row(&mut out, key, r)?;
        }
    }
    out.push_str("\nBY SYMBOL AND ACTION\nSleeve/symbol/action | orders | fills | costed % | slippage bp | fee bp | all-in bp | cost USDT | maker %\n");
    if let Some(rows) = report["metrics"]["actual"].as_object() {
        for (key, r) in rows {
            cost_row(&mut out, key, r)?;
        }
    }
    if let Some(total) = report["metrics"]["actual_by_slice"].get("all") {
        writeln!(out,"\nKnown fees across all identified fills: {} USDT; missing fee: {} fills; missing midpoint: {} fills; unidentified legacy rows: {}.",
            number(&total["known_fee_usdt"],1.0),total["fills_without_fee"],total["fills_without_arrival_mid"],total["unidentified_fill_rows"])?;
    }
    out.push_str("\nCROSSING CALIBRATION: simulated minus actual signed price cost\nSymbol/hop | orders | mean error bp | largest absolute error bp\n");
    if let Some(rows) = report["metrics"]["market_calibration"].as_object() {
        for (key, r) in rows {
            writeln!(
                out,
                "{key} | {} | {} | {}",
                r["orders"],
                number(&r["mean_signed_price_error_bp"], 1.0),
                number(&r["max_absolute_price_error_bp"], 1.0)
            )?;
        }
    }
    out.push_str("\nPAIRED COSTS: 100 ms one-way latency; no queue cancellation credit\nPositive saving favours the candidate. Open and reduce are separate.\nAction/policy | paired orders | saving bp | price bp | fee bp | missed bp | filled % | maker %\n");
    let mut cells = 0;
    if let Some(rows) = report["metrics"]["paired_by_slice"].as_object() {
        for (key, r) in rows {
            if !(key.starts_with("open|") || key.starts_with("reduce|"))
                || !key.ends_with("|TradesOnly|100ms")
            {
                continue;
            }
            cells += 1;
            writeln!(
                out,
                "{} | {} | {} | {} | {} | {} | {} | {}",
                key.trim_end_matches("|TradesOnly|100ms"),
                r["paired_orders"],
                number(&r["saving_vs_cross_bp"], 1.0),
                number(&r["price_cost_bp"], 1.0),
                number(&r["fee_bp"], 1.0),
                number(&r["missed_opportunity_bp"], 1.0),
                number(&r["filled_fraction"], 100.0),
                number(&r["maker_fraction_of_requested"], 100.0)
            )?;
        }
    }
    if cells == 0 {
        out.push_str(
            "No matched complete comparisons at this latency. No winner is established.\n",
        );
    }
    writeln!(
        out,
        "\nMissing data / unavailable arms: {}",
        report["metrics"]["unavailable_order_or_arm_counts"]
    )?;
    out.push_str("All latency/queue cells, per-sleeve/day splits, actual and hypothetical markouts, decision features and input hashes: latest.json.\nExploratory costs; correlated orders and uncertain queues. No live policy is changed.\n");
    Ok(out)
}

fn cost_row(out: &mut String, key: &str, row: &Value) -> Result<(), std::fmt::Error> {
    let cost = &row["costed"];
    writeln!(
        out,
        "{key} | {} | {} | {} | {} | {} | {} | {} | {}",
        row["orders"],
        row["fills"],
        number(&cost["fill_notional_coverage"], 100.0),
        number(&cost["price_shortfall_bp"], 1.0),
        number(&cost["fee_bp"], 1.0),
        number(&cost["total_shortfall_bp"], 1.0),
        number(&cost["total_cost_usdt"], 1.0),
        number(&row["maker_notional_fraction"], 100.0)
    )
}

fn number(value: &Value, scale: f64) -> String {
    value
        .as_f64()
        .map_or_else(|| "unknown".into(), |n| format!("{:.3}", n * scale))
}

fn timestamp(at: u64) -> String {
    let seconds = at / 1_000_000_000;
    let (y, m, d) = super::runner::civil_date((seconds / 86400) as i64);
    format!(
        "{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02} UTC",
        seconds / 3600 % 24,
        seconds / 60 % 60,
        seconds % 60
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn order(
        id: &str,
        side: &str,
        qty: f64,
        price: f64,
        mid: f64,
        fee: Option<f64>,
    ) -> OrderResult {
        serde_json::from_value(json!({
            "observed": {
                "request":{"client_order_id":id,"strategy":0,"symbol":0,"side":side,"qty":qty,"kind":"Market","stop":null,"reduce_only":side=="Sell"},
                "intent":null,"symbol":"XUSDT","sleeve":"long","engine_commit":"fixture",
                "source_segment":0,"source_offset":8,"process_epoch_ms":1,"wire_mono_ns":1,
                "decision_ns":1_000_000_000_u64,"socket_write_ns":null,"transport_rtt_ns":null,
                "arrival_mid":mid,"rule":null,"fills":{id:{"exec_id":id,"at_ns":2_000_000_000_u64,"qty":qty,"price":price,"fee":fee,"maker":false}},
                "unidentified_fill_rows":0,"terminal":null,"amends":0,"cancels":0
            },
            "hypothetical":[],"unavailable":"no_tape","actual_markouts_bp":{}
        })).unwrap()
    }

    #[test]
    fn actual_all_in_uses_dollars_and_the_same_measured_fills_for_both_components() {
        let results = [
            order("buy", "Buy", 2.0, 110.0, 100.0, Some(0.22)),
            order("sell", "Sell", 1.0, 99.0, 100.0, Some(-0.01)),
            order("missing-fee", "Buy", 3.0, 120.0, 100.0, None),
            order("missing-mid", "Buy", 1.0, 50.0, 0.0, Some(5.0)),
        ];
        let m = metrics(&results);
        let total = &m["actual_by_slice"]["all"];
        let cost = &total["costed"];
        assert_eq!(cost["fills"], 2);
        assert_eq!(cost["reference_notional_usdt"], 300.0);
        assert_eq!(cost["filled_notional_usdt"], 319.0);
        assert_eq!(cost["price_cost_usdt"], 21.0);
        assert!((cost["fee_usdt"].as_f64().unwrap() - 0.21).abs() < 1e-12);
        assert!((cost["total_shortfall_bp"].as_f64().unwrap() - 707.0).abs() < 1e-10);
        assert!((cost["fill_notional_coverage"].as_f64().unwrap() - 319.0 / 729.0).abs() < 1e-12);
        assert_eq!(total["fills_without_fee"], 1);
        assert_eq!(total["fills_without_arrival_mid"], 1);
        assert_eq!(
            m["actual_by_order"]["buy"]["costed"]["total_shortfall_bp"],
            1011.0
        );
        assert_eq!(
            m["actual_by_order"]["sell"]["costed"]["total_shortfall_bp"],
            99.0
        );
        assert!(m["actual_by_order"]["missing-fee"]["costed"]["total_shortfall_bp"].is_null());
        assert!(m["actual_by_order"]["missing-mid"]["costed"]["total_shortfall_bp"].is_null());
        assert_eq!(m["actual_by_slice"]["sleeve|long"], *total);
        assert_eq!(m["actual_by_slice"]["day|1970-01-01"], *total);
        let report = json!({"metrics":m});
        let text = text(&report).unwrap();
        assert!(text.contains("slippage bp | fee bp | all-in bp"));
        assert!(text.contains("707.000"));
        assert!(text.contains("Funding is separate"));
    }

    #[test]
    fn empty_and_unmeasured_costs_do_not_report_free_execution() {
        let m = metrics(&[order("blind", "Buy", 1.0, 100.0, 0.0, None)]);
        let cost = &m["actual_by_slice"]["all"]["costed"];
        assert_eq!(cost["fills"], 0);
        assert!(cost["total_cost_usdt"].is_null());
        assert!(cost["total_shortfall_bp"].is_null());
        assert_eq!(cost["fill_notional_coverage"], 0.0);
        assert!(
            metrics(&[])["actual_by_slice"]["all"]["costed"]["fill_notional_coverage"].is_null()
        );
    }
}
