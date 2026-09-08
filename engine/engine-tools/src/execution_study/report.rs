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
        if let Some(reason) = &result.unavailable {
            *failures.entry(reason.clone()).or_default() += 1;
        }
        if matches!(order.request.kind, engine_types::OrderKind::Market) {
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
    let actual:BTreeMap<_,_>=actual.into_iter().map(|(key,orders)|{
        let mut fills=0; let mut notional=0.0; let mut maker_notional=0.0;
        let mut fee=0.0; let mut fee_notional=0.0; let mut unknown_fees=0;
        let mut mark_sum=[0.0;4]; let mut mark_weight=[0.0;4]; let mut mark_counts=[0_u64;4];
        let mut price_cost=0.0; let mut price_weight=0.0;
        let mut rtts=Vec::new(); let mut prewires=Vec::new();
        for result in &orders {
            let o=&result.observed;
            rtts.extend(o.transport_rtt_ns);
            prewires.extend(o.socket_write_ns.zip(o.decision_ns).and_then(|(w,d)|w.checked_sub(d)));
            for (id,f) in &o.fills {
                fills+=1;let w=f.qty*f.price;notional+=w;
                if f.maker {maker_notional+=w;}
                if let Some(value)=f.fee {fee+=value;fee_notional+=w;} else {unknown_fees+=1;}
                if o.arrival_mid>0.0 {
                    let sign=if o.request.side==engine_types::Side::Buy{1.0}else{-1.0};
                    price_cost+=sign*f.qty*(f.price-o.arrival_mid);
                    price_weight+=f.qty*o.arrival_mid;
                }
                if let Some(marks)=result.actual_markouts_bp.get(id) {
                    for i in 0..4 {if let Some(mark)=marks[i]{mark_sum[i]+=mark*w;mark_weight[i]+=w;mark_counts[i]+=1;}}
                }
            }
        }
        rtts.sort_unstable();prewires.sort_unstable();
        let ratio=|a:f64,b:f64|if b>0.0 {Some(a/b)}else{None};
        let percentile=|v:&[u64],p:usize|v.get((v.len().saturating_sub(1))*p/100).map(|n|*n as f64/1e6);
        (key,json!({"orders":orders.len(),"fills":fills,"filled_notional_usdt":notional,
            "maker_notional_fraction":ratio(maker_notional,notional),"known_fee_usdt":fee,
            "known_fee_bp":ratio(fee*1e4,fee_notional),"fills_without_fee":unknown_fees,
            "filled_price_shortfall_bp":ratio(price_cost*1e4,price_weight),
            "markout_horizons_s":[1,15,60,300],"signed_markouts_bp":(0..4).map(|i|ratio(mark_sum[i],mark_weight[i])).collect::<Vec<_>>(),"markout_fill_counts":mark_counts,
            "transport_rtt_p50_ms":percentile(&rtts,50),"transport_rtt_p90_ms":percentile(&rtts,90),
            "decision_to_socket_p50_ms":percentile(&prewires,50),"decision_to_socket_p90_ms":percentile(&prewires,90)
        }))
    }).collect();
    let calibration:BTreeMap<_,_>=calibration.into_iter().map(|(key,errors)|(key,json!({
        "orders":errors.len(),"mean_signed_price_error_bp":errors.iter().sum::<f64>()/errors.len() as f64,
        "mean_absolute_price_error_bp":errors.iter().map(|e|e.abs()).sum::<f64>()/errors.len() as f64,
        "max_absolute_price_error_bp":errors.iter().map(|e|e.abs()).fold(0.0,f64::max)
    }))).collect();
    json!({"actual":actual,"paired_by_slice":paired,"market_calibration":calibration,"unavailable_order_or_arm_counts":failures})
}

pub fn text(report: &Value) -> Result<String, std::fmt::Error> {
    let mut out=format!("One-sided execution study\nGenerated: {} | commit: {}\nWindow starts: {} | aligned orders: {} | cached: {}\n\n",
        timestamp(report["generated_ns"].as_u64().unwrap_or_default()),report["code_commit"].as_str().unwrap_or("unknown"),timestamp(report["window_start_ns"].as_u64().unwrap_or_default()),report["orders"].as_array().map_or(0,Vec::len),report["cached_orders"]);
    out.push_str("ACTUAL FILLS (WAL-matched orders; not whole-account P&L)\nSleeve/symbol/action | orders | fills | notional USDT | fee bp | maker %\n");
    if let Some(rows) = report["metrics"]["actual"].as_object() {
        for (key, r) in rows {
            writeln!(
                out,
                "{key} | {} | {} | {:.2} | {} | {}",
                r["orders"],
                r["fills"],
                r["filled_notional_usdt"].as_f64().unwrap_or_default(),
                number(&r["known_fee_bp"], 1.0),
                number(&r["maker_notional_fraction"], 100.0)
            )?;
        }
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
