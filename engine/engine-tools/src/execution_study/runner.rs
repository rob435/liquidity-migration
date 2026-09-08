//! Scheduled local study: account fee observations, incremental orders, paired tape replay.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};

use super::sim::{Fees, Outcome, Policy, QueueModel, Trial};
use super::{observed, ObservedOrder, ObservedState, Result};
use crate::backtest::tape::{BookBuilder, TapeReader, TapeRow};

const SECOND: u64 = 1_000_000_000;
const HOUR: u64 = 3600 * SECOND;

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub schema: u32,
    pub wal_path: PathBuf,
    pub tape_root: PathBuf,
    pub output_dir: PathBuf,
    pub lookback_hours: u64,
    pub hop_ms: Vec<u64>,
    pub policies: Vec<Policy>,
    pub sleeves: Vec<String>,
    pub fee_realm: String,
    #[serde(default)]
    pub fee_snapshot_path: Option<PathBuf>,
}

#[derive(Serialize, Deserialize, Default)]
struct FeeSnapshot {
    account_id: String,
    realm: String,
    rates: BTreeMap<String, Fees>,
    #[serde(default)]
    unavailable: BTreeMap<String, String>,
}

#[derive(Clone, Serialize, Deserialize)]
pub(super) struct OrderResult {
    pub observed: ObservedOrder,
    pub hypothetical: Vec<Outcome>,
    pub unavailable: Option<String>,
    pub actual_markouts_bp: BTreeMap<String, [Option<f64>; 4]>,
}

#[derive(Serialize, Deserialize)]
struct CachedOrder {
    fingerprint: String,
    result: OrderResult,
    sources: BTreeMap<String, String>,
}

#[derive(Default, Serialize, Deserialize)]
struct Cache {
    schema: u32,
    orders: BTreeMap<String, CachedOrder>,
}

pub fn run(path: &Path) -> Result<()> {
    let config_bytes = fs::read(path)?;
    let config: Config = serde_json::from_slice(&config_bytes)?;
    if config.schema != 1
        || config.lookback_hours == 0
        || config.lookback_hours > 168
        || config.hop_ms.is_empty()
        || config.hop_ms.iter().any(|h| *h == 0 || *h > 5000)
        || config.policies.is_empty()
        || !config.policies.contains(&Policy::Cross)
        || config.sleeves.is_empty()
    {
        return Err(
            "invalid execution-study config; include cross and positive bounded horizons".into(),
        );
    }
    fs::create_dir_all(&config.output_dir)?;
    let now = engine_core::clock::wall_ns();
    let since = now.saturating_sub(config.lookback_hours * HOUR);
    let state_path = config.output_dir.join("observed.json");
    let mut state: ObservedState = if state_path.exists() {
        serde_json::from_slice(&fs::read(&state_path)?)?
    } else {
        ObservedState::default()
    };
    observed::scan(&config.wal_path, since, &mut state)?;
    state.orders.retain(|_, order| {
        order.decision_ns.map_or_else(
            || order.source_segment.saturating_add(2) >= state.segment,
            |at| at >= since,
        )
    });
    atomic_json(&state_path, &state)?;
    let selected: Vec<_> = state
        .orders
        .values()
        .filter(|order| {
            order.decision_ns.is_some_and(|at| at >= since)
                && config.sleeves.contains(&order.sleeve)
        })
        .collect();
    let symbols: BTreeSet<_> = selected.iter().map(|o| o.symbol.clone()).collect();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?;
    let fees = runtime.block_on(refresh_fees(&config, &symbols, now))?;
    let config_hash = hex::encode(Sha256::digest(&config_bytes));
    let cache_path = config.output_dir.join("replay-cache.json");
    let mut cache: Cache = if cache_path.exists() {
        serde_json::from_slice(&fs::read(&cache_path)?)?
    } else {
        Cache::default()
    };
    if cache.schema != 1 {
        cache = Cache {
            schema: 1,
            ..Cache::default()
        };
    }
    cache.orders.retain(|id, _| state.orders.contains_key(id));
    let mut cache_hits = 0;
    let mut results = Vec::new();
    let mut source_files = BTreeMap::new();
    let mut symbols_report = BTreeMap::new();
    for symbol in symbols {
        let all_orders: Vec<_> = selected
            .iter()
            .copied()
            .filter(|o| o.symbol == symbol)
            .collect();
        let Some(rate) = fees.rates.get(&symbol).copied() else {
            for order in all_orders {
                results.push(OrderResult {
                    observed: order.clone(),
                    hypothetical: vec![],
                    unavailable: Some("missing_account_fee_rate".into()),
                    actual_markouts_bp: BTreeMap::new(),
                });
            }
            continue;
        };
        let mut orders = Vec::new();
        let mut fingerprints = BTreeMap::new();
        for order in all_orders {
            let fingerprint = hex::encode(Sha256::digest(serde_json::to_vec(&json!({
                "order":order,"maker":rate.maker,"taker":rate.taker,
                "config":config_hash,"commit":engine_core::engine::ENGINE_COMMIT
            }))?));
            let id = &order.request.client_order_id;
            if let Some(cached) = cache
                .orders
                .get(id)
                .filter(|c| c.fingerprint == fingerprint)
            {
                results.push(cached.result.clone());
                source_files.extend(cached.sources.clone());
                cache_hits += 1;
            } else {
                fingerprints.insert(id.clone(), fingerprint);
                orders.push(order);
            }
        }
        if orders.is_empty() {
            continue;
        }
        let first = orders
            .iter()
            .filter_map(|o| o.decision_ns)
            .min()
            .expect("selected clocks");
        let last = orders
            .iter()
            .filter_map(|o| o.decision_ns)
            .max()
            .expect("selected clocks")
            + 480 * SECOND;
        let mut files = BTreeMap::new();
        for root in [config.output_dir.join("tape"), config.tape_root.clone()] {
            for path in files_for(&root, &symbol, first.saturating_sub(HOUR), last, now)? {
                files.insert(path.strip_prefix(&root)?.to_path_buf(), path);
            }
        }
        let mut trials = Vec::new();
        let base = results.len();
        for (index, order) in orders.iter().enumerate() {
            let mut unavailable = None;
            for policy in &config.policies {
                for queue in [QueueModel::TradesOnly, QueueModel::CancellationsAhead] {
                    for hop in &config.hop_ms {
                        match Trial::new(order, *policy, queue, *hop, rate) {
                            Ok(trial) => trials.push((base + index, trial)),
                            Err(error) => unavailable = Some(error.to_string()),
                        }
                    }
                }
            }
            results.push(OrderResult {
                observed: (*order).clone(),
                hypothetical: Vec::new(),
                unavailable,
                actual_markouts_bp: order
                    .fills
                    .keys()
                    .map(|id| (id.clone(), [None; 4]))
                    .collect(),
            });
        }
        let mut builder = BookBuilder::default();
        let mut last_at = 0;
        let mut books = 0_u64;
        let mut trades = 0_u64;
        let mut symbol_sources = BTreeMap::new();
        let tape_result: Result<()> = (|| {
            for file in files.into_values() {
                symbol_sources.insert(file.display().to_string(), file_hash(&file)?);
                let mut reader = TapeReader::open(&file)
                    .map_err(|error| format!("{}: {error}", file.display()))?;
                while let Some((at, row)) = reader
                    .next_row()
                    .map_err(|error| format!("{}: {error}", file.display()))?
                {
                    if at < last_at {
                        return Err(format!("symbol tape regressed in {}", file.display()).into());
                    }
                    last_at = at;
                    let row_symbol = match &row {
                        TapeRow::Book(r) => &r.symbol,
                        TapeRow::Trade(r) => &r.symbol,
                        TapeRow::Ticker(r) => &r.symbol,
                    };
                    if row_symbol != &symbol {
                        return Err(format!(
                            "unexpected symbol {row_symbol} in {}",
                            file.display()
                        )
                        .into());
                    }
                    let depth = builder.is_valid().then(|| *builder.depth());
                    for (_, trial) in &mut trials {
                        if at > trial.start_ns + 480 * SECOND {
                            continue;
                        }
                        while let Some(wake) = trial.next_wake().filter(|wake| *wake <= at) {
                            trial.advance(wake, depth.as_ref());
                        }
                    }
                    match row {
                        TapeRow::Book(book) if book.depth == 50 => {
                            books += 1;
                            let depth = builder.apply(&book).copied();
                            if let Some(d) =
                                depth.as_ref().filter(|d| d.bid_len > 0 && d.ask_len > 0)
                            {
                                let mid = (d.bids[0].px + d.asks[0].px) / 2.0;
                                for result in &mut results[base..] {
                                    let sign = if result.observed.request.side
                                        == engine_types::Side::Buy
                                    {
                                        1.0
                                    } else {
                                        -1.0
                                    };
                                    for (id, fill) in &result.observed.fills {
                                        let marks = result
                                            .actual_markouts_bp
                                            .get_mut(id)
                                            .expect("fill keys initialized");
                                        for (index, seconds) in
                                            [1, 15, 60, 300].into_iter().enumerate()
                                        {
                                            let due = fill.at_ns + seconds * SECOND;
                                            if at >= due
                                                && at - due <= 2 * SECOND
                                                && marks[index].is_none()
                                            {
                                                marks[index] = Some(
                                                    sign * (mid - fill.price) / fill.price * 1e4,
                                                );
                                            }
                                        }
                                    }
                                }
                            }
                            for (_, trial) in &mut trials {
                                if at <= trial.start_ns + 480 * SECOND {
                                    trial.book(at, depth.as_ref());
                                }
                            }
                        }
                        TapeRow::Trade(trade) => {
                            trades += 1;
                            let depth = builder.is_valid().then(|| builder.depth());
                            for (_, trial) in &mut trials {
                                if at + 30 * SECOND >= trial.start_ns
                                    && at <= trial.start_ns + 480 * SECOND
                                {
                                    trial.trade(
                                        at,
                                        trade.price,
                                        trade.qty,
                                        trade.buyer_aggressor,
                                        depth,
                                    );
                                }
                            }
                        }
                        _ => {}
                    }
                }
            }
            Ok(())
        })();
        let tape_error = tape_result.err().map(|error| error.to_string());
        for (index, trial) in trials {
            let mut outcome = trial.outcome();
            if tape_error.is_some() {
                outcome.incomplete = Some("symbol_tape_error".into());
                outcome.total_shortfall_bp = None;
                outcome.missed_opportunity_bp = None;
                outcome.mark_ns = None;
                for fill in &mut outcome.fills {
                    fill.signed_markouts_bp = [None; 4];
                }
            }
            results[index].hypothetical.push(outcome);
        }
        if let Some(error) = &tape_error {
            for result in &mut results[base..] {
                result.unavailable = Some(error.clone());
                for marks in result.actual_markouts_bp.values_mut() {
                    *marks = [None; 4];
                }
            }
        }
        for result in &results[base..] {
            if !result.hypothetical.is_empty()
                && result.hypothetical.iter().all(|o| o.incomplete.is_none())
                && result
                    .observed
                    .decision_ns
                    .is_some_and(|at| at + 480 * SECOND < now / HOUR * HOUR)
            {
                let id = result.observed.request.client_order_id.clone();
                cache.orders.insert(
                    id.clone(),
                    CachedOrder {
                        fingerprint: fingerprints[&id].clone(),
                        result: result.clone(),
                        sources: symbol_sources.clone(),
                    },
                );
            }
        }
        source_files.extend(symbol_sources);
        symbols_report.insert(
            symbol,
            json!({"book_rows":books,"trade_rows":trades,"last_tape_ns":last_at,"input_error":tape_error}),
        );
    }
    results.sort_by_key(|r| r.observed.decision_ns);
    atomic_json(&cache_path, &cache)?;
    let mut paired: BTreeMap<String, Vec<(f64, f64)>> = BTreeMap::new();
    for result in &results {
        for candidate in &result.hypothetical {
            let baseline = result.hypothetical.iter().find(|o| {
                o.policy == Policy::Cross
                    && o.queue_model == candidate.queue_model
                    && o.hop_ms == candidate.hop_ms
            });
            if let Some((cross, other)) =
                baseline.and_then(|b| b.total_shortfall_bp.zip(candidate.total_shortfall_bp))
            {
                let key = format!(
                    "{}|{:?}|{}ms",
                    candidate.policy.name(),
                    candidate.queue_model,
                    candidate.hop_ms
                );
                paired
                    .entry(key)
                    .or_default()
                    .push((cross - other, candidate.requested_notional));
            }
        }
    }
    let paired: BTreeMap<_,_>=paired.into_iter().map(|(key,rows)|{
        let notional:f64=rows.iter().map(|r|r.1).sum();
        (key,json!({"paired_orders":rows.len(),"requested_notional_usdt":notional,"saving_vs_cross_bp":rows.iter().map(|(v,w)|v*w).sum::<f64>()/notional}))
    }).collect();
    let metrics = super::report::metrics(&results);
    let report = json!({
        "schema":1,"generated_ns":now,"code_commit":engine_core::engine::ENGINE_COMMIT,"config_sha256":config_hash,
        "config":config,"window_start_ns":since,"wal_cursor":{"segment":state.segment,"offset":state.offset,"records_read":state.records_read},
        "observed_orders":state.orders.len(),"orders_without_aligned_clock":state.orders.values().filter(|o|o.decision_ns.is_none()).count(),
        "fee_snapshot":fees,"symbols":symbols_report,"source_files_sha256":source_files,"paired":paired,"orders":results,
        "cached_orders":cache_hits,"metrics":metrics,
        "interpretation":{
            "class":"exploratory one-sided execution diagnostic; no strategy return or promotion claim",
            "sign":"positive shortfall is a cost; positive saving_vs_cross_bp favours the candidate",
            "benchmark":"all requested quantity at observed arrival midpoint; unfilled quantity marked at the common decision+180s horizon",
            "fee_scope":"account fee snapshot is applied equally to hypothetical arms as a pricing scenario; actual fills retain their original fees",
            "queue":"trades_only gives no cancellation credit; cancellations_ahead assigns inferred cancellation to the queue ahead; neither is an exact venue queue",
            "latency":"actual decision-to-socket delay plus each configured hypothetical one-way hop; local recorder receive time, not matching-engine time",
            "limits":["Orders are independent marginal counterfactuals; overlapping intentions are not a joint inventory simulation.","No market response to our hypothetical orders, hidden liquidity or cross-order quota model.","Only L50 books and finite aggressive trade size produce hypothetical fills; public book touch alone does not fill.","Protective exits and strategy deadlines are not licensed to wait by this study.","Recorded tape used to choose parameters is seen data; subsequent days grade a committed rule.","Only closed recorder hours are read; current-hour results remain incomplete until a later run.","Missing observations stay unscored. Paired-order counts are not independent statistical samples."]
        }
    });
    atomic_json(&config.output_dir.join("latest.json"), &report)?;
    let summary = super::report::text(&report)?;
    let archive = config
        .output_dir
        .join("orders")
        .join(&config_hash)
        .join(engine_core::engine::ENGINE_COMMIT);
    fs::create_dir_all(&archive)?;
    for result in &results {
        if let Some(at) = result
            .observed
            .decision_ns
            .filter(|at| at + 480 * SECOND < now / HOUR * HOUR)
        {
            let id_hash = hex::encode(Sha256::digest(
                result.observed.request.client_order_id.as_bytes(),
            ));
            atomic_json(
                &archive.join(format!("{}-{id_hash}.json", at / (24 * HOUR))),
                &json!({
                    "schema":1,"generated_ns":now,"code_commit":engine_core::engine::ENGINE_COMMIT,
                    "config":config,"config_sha256":config_hash,"fee_snapshot":fees,
                    "source_files_sha256":source_files,"result":result
                }),
            )?;
        }
    }
    fs::write(config.output_dir.join("latest.txt"), &summary)?;
    print!("{summary}");
    Ok(())
}

#[cfg(feature = "bybit")]
async fn refresh_fees(
    config: &Config,
    symbols: &BTreeSet<String>,
    now: u64,
) -> Result<FeeSnapshot> {
    if let Some(path) = &config.fee_snapshot_path {
        let snapshot: FeeSnapshot = serde_json::from_slice(&fs::read(path)?)?;
        if snapshot.realm != config.fee_realm {
            return Err("offline fee snapshot realm differs from the study".into());
        }
        return Ok(snapshot);
    }
    use engine_venue::venues::bybit::{BybitInventoryProbe, VenueRealm};
    let path = config.output_dir.join("fees.json");
    let mut snapshot: FeeSnapshot = if path.exists() {
        serde_json::from_slice(&fs::read(&path)?)?
    } else {
        FeeSnapshot::default()
    };
    if symbols.is_empty() {
        return Ok(snapshot);
    }
    let realm = match config.fee_realm.as_str() {
        "mainnet" => VenueRealm::Mainnet,
        "demo" => VenueRealm::Demo,
        _ => return Err("unsupported fee realm".into()),
    };
    let mut probe = BybitInventoryProbe::new(realm)?;
    let who = probe.account_identity().await?;
    let expected = std::env::var("EXPECTED_ENGINE_ACCOUNT_USER_ID")?;
    if who.user_id != expected || who.realm != config.fee_realm {
        return Err("execution-study fee account identity mismatch".into());
    }
    if !snapshot.account_id.is_empty()
        && (snapshot.account_id != who.user_id || snapshot.realm != config.fee_realm)
    {
        return Err("execution-study fee cache belongs to another account".into());
    }
    snapshot.account_id = who.user_id;
    snapshot.realm = config.fee_realm.clone();
    for symbol in symbols {
        if snapshot
            .rates
            .get(symbol)
            .is_some_and(|f| f.observed_ns <= now && now - f.observed_ns < 24 * HOUR)
        {
            continue;
        }
        let (maker, taker) = match probe.fee_rates(symbol).await {
            Ok(rates) => rates,
            Err(error) => {
                snapshot.rates.remove(symbol);
                snapshot
                    .unavailable
                    .insert(symbol.clone(), error.to_string());
                continue;
            }
        };
        snapshot.unavailable.remove(symbol);
        snapshot.rates.insert(
            symbol.clone(),
            Fees {
                maker,
                taker,
                observed_ns: engine_core::clock::wall_ns(),
            },
        );
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;
    }
    atomic_json(&path, &snapshot)?;
    Ok(snapshot)
}

#[cfg(not(feature = "bybit"))]
async fn refresh_fees(_: &Config, _: &BTreeSet<String>, _: u64) -> Result<FeeSnapshot> {
    Err("execution-study requires the Bybit feature".into())
}

fn files_for(root: &Path, symbol: &str, start: u64, end: u64, now: u64) -> Result<Vec<PathBuf>> {
    if symbol.is_empty() || !symbol.bytes().all(|b| b.is_ascii_alphanumeric()) {
        return Err("invalid tape symbol".into());
    }
    let mut files = Vec::new();
    for hour in start / HOUR..=end / HOUR {
        if hour >= now / HOUR {
            continue;
        }
        let (year, month, day) = civil_date((hour / 24) as i64);
        let directory = root
            .join(format!("{year:04}-{month:02}-{day:02}"))
            .join(format!("{:02}", hour % 24))
            .join(symbol);
        if !directory.exists() {
            continue;
        }
        let mut found: BTreeMap<String, PathBuf> = BTreeMap::new();
        for entry in fs::read_dir(directory)? {
            let path = entry?.path();
            let name = path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .into_owned();
            if name.starts_with("segment-")
                && (name.ends_with(".jsonl") || name.ends_with(".jsonl.zst"))
            {
                let key = name.trim_end_matches(".zst").to_string();
                if name.ends_with(".zst") || !found.contains_key(&key) {
                    found.insert(key, path);
                }
            }
        }
        files.extend(found.into_values());
    }
    Ok(files)
}

pub(super) fn civil_date(days: i64) -> (i64, i64, i64) {
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = mp + if mp < 10 { 3 } else { -9 };
    (y + i64::from(m <= 2), m, d)
}

fn file_hash(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path)?;
    let mut hash = Sha256::new();
    let mut buffer = [0; 64 * 1024];
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
    }
    Ok(hex::encode(hash.finalize()))
}

pub fn atomic_json(path: &Path, value: &impl Serialize) -> Result<()> {
    let temporary = path.with_extension(format!("{}.tmp", std::process::id()));
    fs::write(&temporary, serde_json::to_vec(value)?)?;
    fs::rename(temporary, path)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::io::Write;

    fn append(file: &mut fs::File, row: serde_json::Value) {
        let bytes = serde_json::to_vec(&row).unwrap();
        file.write_all(&(bytes.len() as u32).to_le_bytes()).unwrap();
        file.write_all(&crc32c::crc32c(&bytes).to_le_bytes())
            .unwrap();
        file.write_all(&bytes).unwrap();
    }

    #[test]
    fn shipped_config_parses_and_hour_paths_cover_leap_days() {
        let config: Config = serde_json::from_str(include_str!(
            "../../../../configs/execution_study_mainnet_v1.json"
        ))
        .unwrap();
        assert_eq!(config.policies.len(), 8);
        assert_eq!(civil_date(0), (1970, 1, 1));
        assert_eq!(civil_date(19782), (2024, 2, 29));
    }

    #[test]
    fn complete_command_pairs_real_order_identity_and_costs_with_all_tape_arms() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let at = (engine_core::clock::wall_ns() / HOUR - 2) * HOUR + 60 * SECOND;
        let family = root.join("engine.wal");
        let mut wal = fs::File::create(&family).unwrap();
        wal.write_all(b"EWAL0001").unwrap();
        append(
            &mut wal,
            json!({"kind":"names","symbols":["XUSDT"],"strategies":["long"]}),
        );
        append(
            &mut wal,
            json!({"kind":"instrument_catalog_checkpoint","checkpoint":{"rules":[["XUSDT",{"tick_size":1.0,"qty_step":1.0,"min_qty":1.0,"min_notional":1.0}]]}}),
        );
        append(
            &mut wal,
            json!({"kind":"order_sent_v2","request":{"client_order_id":"a","strategy":0,"symbol":0,"side":"Buy","qty":2.0,"kind":"Market","stop":null,"reduce_only":false},"wire_ns":100,"arrival_mid":100.0}),
        );
        append(
            &mut wal,
            json!({"kind":"venue_timing","operation":"place","client_order_id":"a","socket_write_ns":100,"ack_ns":150,"core_handled_ns":200,"core_handled_wall_ns":at+100}),
        );
        for (id, qty, fee) in [("one", 0.5, None), ("two", 1.5, Some(0.1515))] {
            append(
                &mut wal,
                json!({"kind":"order_update_v3","update":{"Fill":{
                    "exec_id":id,"client_order_id":"a","symbol":0,"side":"Buy","qty":qty,
                    "px":101.0,"fee":fee,"is_maker":false,"venue_ts_ms":at/1_000_000+25,"recv_ns":200
                }}}),
            );
        }
        wal.flush().unwrap();
        let tape_root = root.join("tape");
        let hour = at / HOUR;
        let (y, m, d) = civil_date((hour / 24) as i64);
        let directory = tape_root
            .join(format!("{y:04}-{m:02}-{d:02}"))
            .join(format!("{:02}", hour % 24))
            .join("XUSDT");
        fs::create_dir_all(&directory).unwrap();
        let mut tape = fs::File::create(directory.join("segment-000000.jsonl")).unwrap();
        for second in 0..=481_u64 {
            let stamp = if second == 0 {
                at - 1_000_000
            } else {
                at + second * SECOND
            };
            let row = json!({"kind":"orderbook_snapshot","venue":"bybit","symbol":"XUSDT","depth":50,"local_receive_ts_ns":stamp,"exchange_system_ts_ns":stamp,"update_id":second+1,"cross_sequence":second+1,"bids":[["99","10"]],"asks":[["101","10"]]});
            writeln!(tape, "{row}").unwrap();
            if second == 1 {
                writeln!(tape,"{}",json!({"kind":"public_trade","venue":"bybit","symbol":"XUSDT","local_receive_ts_ns":stamp+1,"exchange_trade_ts_ns":stamp+1,"price":"99","qty":"20","side":"Sell"})).unwrap();
            }
        }
        tape.flush().unwrap();
        let fees = root.join("fees.json");
        atomic_json(&fees,&json!({"account_id":"fixture","realm":"mainnet","rates":{"XUSDT":{"maker":0.00036,"taker":0.001,"observed_ns":at}}})).unwrap();
        let config = Config {
            schema: 1,
            wal_path: family,
            tape_root,
            output_dir: root.join("out"),
            lookback_hours: 24,
            hop_ms: vec![25, 100, 250],
            policies: vec![
                Policy::Cross,
                Policy::Current,
                Policy::PostOnly5s,
                Policy::PostOnly30s,
                Policy::PostOnly120s,
                Policy::Adaptive120s,
                Policy::PassiveSkip120s,
            ],
            sleeves: vec!["long".into()],
            fee_realm: "mainnet".into(),
            fee_snapshot_path: Some(fees),
        };
        let path = root.join("config.json");
        atomic_json(&path, &config).unwrap();
        run(&path).unwrap();
        let report: serde_json::Value =
            serde_json::from_slice(&fs::read(config.output_dir.join("latest.json")).unwrap())
                .unwrap();
        let arms = report["orders"][0]["hypothetical"].as_array().unwrap();
        let actual = &report["metrics"]["actual"]["long|XUSDT|open"];
        assert_eq!(actual["fills_without_fee"], 1);
        assert_eq!(actual["known_fee_bp"], 10.0);
        assert_eq!(actual["markout_fill_counts"], json!([2, 2, 2, 2]));
        assert_eq!(
            report["metrics"]["market_calibration"]["XUSDT|25ms"]["max_absolute_price_error_bp"],
            0.0
        );
        assert_eq!(arms.len(), 42);
        assert!(
            arms.iter().all(|arm| arm["incomplete"].is_null()),
            "{arms:?}"
        );
        for arm in arms {
            let expected = if ["cross", "current"].contains(&arm["policy"].as_str().unwrap()) {
                110.1
            } else {
                -96.436
            };
            assert!(
                (arm["total_shortfall_bp"].as_f64().unwrap() - expected).abs() < 1e-8,
                "{arm}"
            );
        }
        let cursor = report["wal_cursor"].clone();
        let healthy_tape = fs::read_to_string(directory.join("segment-000000.jsonl")).unwrap();
        fs::remove_dir_all(&config.tape_root).unwrap();
        run(&path).unwrap();
        let again: serde_json::Value =
            serde_json::from_slice(&fs::read(config.output_dir.join("latest.json")).unwrap())
                .unwrap();
        assert_eq!(again["wal_cursor"], cursor);
        assert_eq!(again["orders"], report["orders"]);
        assert_eq!(again["cached_orders"], 1);
        assert_eq!(again["source_files_sha256"], report["source_files_sha256"]);

        append(
            &mut wal,
            json!({"kind":"names","symbols":["XUSDT","YUSDT"],"strategies":["long"]}),
        );
        append(
            &mut wal,
            json!({"kind":"instrument_catalog_checkpoint","checkpoint":{"rules":[["YUSDT",{"tick_size":1.0,"qty_step":1.0,"min_qty":1.0,"min_notional":1.0}]]}}),
        );
        append(
            &mut wal,
            json!({"kind":"order_sent_v2","request":{"client_order_id":"bad-tape","strategy":0,"symbol":1,"side":"Buy","qty":2.0,"kind":"Market","stop":null,"reduce_only":false},"wire_ns":100,"arrival_mid":100.0}),
        );
        append(
            &mut wal,
            json!({"kind":"venue_timing","operation":"place","client_order_id":"bad-tape","socket_write_ns":100,"ack_ns":150,"core_handled_ns":200,"core_handled_wall_ns":at+100}),
        );
        wal.flush().unwrap();
        let rate_path = config.fee_snapshot_path.as_ref().unwrap();
        let mut rates: Value = serde_json::from_slice(&fs::read(rate_path).unwrap()).unwrap();
        rates["rates"]["YUSDT"] = rates["rates"]["XUSDT"].clone();
        atomic_json(rate_path, &rates).unwrap();
        let bad_directory = directory.with_file_name("YUSDT");
        fs::create_dir_all(&bad_directory).unwrap();
        let bad_path = bad_directory.join("segment-000000.jsonl");
        let repaired = healthy_tape.replace("XUSDT", "YUSDT");
        let mut reversed: Vec<_> = repaired.lines().take(2).collect();
        reversed.reverse();
        fs::write(&bad_path, reversed.join("\n") + "\n").unwrap();
        run(&path).expect("one corrupt symbol must not suppress the report");
        let partial: Value =
            serde_json::from_slice(&fs::read(config.output_dir.join("latest.json")).unwrap())
                .unwrap();
        assert_eq!(partial["cached_orders"], 1);
        assert!(partial["symbols"]["YUSDT"]["input_error"]
            .as_str()
            .unwrap()
            .contains("before the previous row"));
        assert!(partial["source_files_sha256"]
            .get(bad_path.display().to_string())
            .is_some());
        let bad = partial["orders"]
            .as_array()
            .unwrap()
            .iter()
            .find(|o| o["observed"]["symbol"] == "YUSDT")
            .unwrap();
        assert_eq!(bad["hypothetical"].as_array().unwrap().len(), 42);
        assert!(bad["hypothetical"]
            .as_array()
            .unwrap()
            .iter()
            .all(|o| o["total_shortfall_bp"].is_null() && o["incomplete"] == "symbol_tape_error"));
        assert_eq!(
            partial["metrics"]["paired_by_slice"],
            report["metrics"]["paired_by_slice"]
        );

        fs::write(&bad_path, repaired).unwrap();
        run(&path).unwrap();
        let repaired: Value =
            serde_json::from_slice(&fs::read(config.output_dir.join("latest.json")).unwrap())
                .unwrap();
        assert_eq!(repaired["cached_orders"], 1);
        assert!(repaired["symbols"]["YUSDT"]["input_error"].is_null());
        assert!(repaired["orders"]
            .as_array()
            .unwrap()
            .iter()
            .all(|o| o["hypothetical"]
                .as_array()
                .unwrap()
                .iter()
                .all(|a| a["incomplete"].is_null())));
    }
}
