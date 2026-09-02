use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use engine_types::{Feed, MarketEvent, MarketFeed, Subscription};
use market_tape::config::CaptureConfig;
use market_tape::schema::{book_row_from_event, ticker_row_from_event, trade_row_from_event};
use market_tape::storage::{write_status_json, SegmentWriter};

fn now_epoch_ns() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos() as i64
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let command = args.next().unwrap_or_else(|| "help".to_string());

    let mut config_path: Option<PathBuf> = None;
    let mut root_override: Option<PathBuf> = None;

    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--config" => config_path = args.next().map(PathBuf::from),
            "--root" => root_override = args.next().map(PathBuf::from),
            _ => {}
        }
    }

    match command.as_str() {
        "check" => {
            let path = config_path.ok_or("missing --config <path>")?;
            let config = CaptureConfig::load_from_file(&path)?;
            let symbols = config.static_symbols(Path::new("."));
            println!(
                "valid capture configuration: venue={} market={} tiers={} static_symbols={}",
                config.venue.name,
                config.venue.market,
                config.tiers.len(),
                symbols.len()
            );
            Ok(())
        }
        "record" => {
            let path = config_path.ok_or("missing --config <path>")?;
            let config = CaptureConfig::load_from_file(&path)?;
            let root = root_override.unwrap_or(config.storage.root.clone());
            let max_bytes = config.storage.segment_max_mb.saturating_mul(1024 * 1024);

            run_recorder(config, root, max_bytes).await
        }
        _ => {
            eprintln!("Usage: market-tape <record|check> --config <path> [--root <path>]");
            std::process::exit(2);
        }
    }
}

async fn run_recorder(
    config: CaptureConfig,
    root: PathBuf,
    max_bytes: u64,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut symbols = config.static_symbols(Path::new("."));
    if symbols.is_empty() {
        symbols.insert("BTCUSDT".to_string());
        symbols.insert("ETHUSDT".to_string());
        symbols.insert("SOLUSDT".to_string());
    }

    let mut subscriptions = Vec::new();
    for sym in &symbols {
        subscriptions.push(Subscription {
            symbol: sym.clone(),
            feed: Feed::Depth,
        });
        subscriptions.push(Subscription {
            symbol: sym.clone(),
            feed: Feed::Trades,
        });
        subscriptions.push(Subscription {
            symbol: sym.clone(),
            feed: Feed::Ticker,
        });
    }

    let mut segment_writer = SegmentWriter::new(&root, max_bytes)?;
    let mut feed = engine_marketdata::BybitPublicFeed::new(&subscriptions);

    let last_receive_ns = Arc::new(AtomicI64::new(now_epoch_ns()));
    let inbound_bytes = Arc::new(AtomicU64::new(0));
    let shutdown_requested = Arc::new(AtomicBool::new(false));

    let shutdown_rx = shutdown_requested.clone();
    tokio::spawn(async move {
        let _ = tokio::signal::ctrl_c().await;
        shutdown_rx.store(true, Ordering::SeqCst);
    });

    let status_interval = Duration::from_secs(config.storage.status_interval_seconds.max(5));
    let mut status_ticker = tokio::time::interval(status_interval);

    let root_for_status = root.clone();
    let venue_name = config.venue.name.clone();
    let market_name = config.venue.market.clone();
    let last_rx = last_receive_ns.clone();
    let in_bytes = inbound_bytes.clone();

    println!(
        "market-tape recorder active: venue={} market={} root={} symbols={}",
        venue_name,
        market_name,
        root.display(),
        symbols.len()
    );

    loop {
        if shutdown_requested.load(Ordering::SeqCst) {
            println!("market-tape shutting down: flushing segments...");
            segment_writer.flush_all()?;
            let payload = serde_json::json!({
                "pid": std::process::id(),
                "venue": venue_name,
                "market": market_name,
                "last_receive_ns": last_rx.load(Ordering::Relaxed),
                "inbound_bytes": in_bytes.load(Ordering::Relaxed),
                "projected_monthly_gb": 0.0,
                "dropped_frames": 0,
                "status": "stopped"
            });
            let _ = write_status_json(&root_for_status, &payload);
            break;
        }

        tokio::select! {
            _ = status_ticker.tick() => {
                let bytes = in_bytes.load(Ordering::Relaxed);
                let payload = serde_json::json!({
                    "pid": std::process::id(),
                    "venue": venue_name,
                    "market": market_name,
                    "last_receive_ns": last_rx.load(Ordering::Relaxed),
                    "inbound_bytes": bytes,
                    "projected_monthly_gb": (bytes as f64) / (1024.0 * 1024.0 * 1024.0),
                    "dropped_frames": 0,
                    "status": "recording"
                });
                let _ = write_status_json(&root_for_status, &payload);
            }
            event_res = feed.next_event() => {
                let event = match event_res {
                    Ok(event) => event,
                    Err(engine_types::FeedError::Closed) => break,
                    Err(err) => {
                        eprintln!("market feed transport warning: {err}");
                        continue;
                    }
                };

                let now_ns = now_epoch_ns();
                last_rx.store(now_ns, Ordering::Relaxed);

                let (symbol_name, record_bytes) = match event {
                    MarketEvent::Depth { symbol, depth } => {
                        let name = feed.symbols().name(symbol);
                        let row = book_row_from_event(&venue_name, name, depth, now_ns);
                        let bytes = serde_json::to_vec(&row).unwrap();
                        (name.to_string(), bytes)
                    }
                    MarketEvent::Trades { symbol, trades } => {
                        let name = feed.symbols().name(symbol);
                        let row = trade_row_from_event(&venue_name, name, trades, now_ns);
                        let bytes = serde_json::to_vec(&row).unwrap();
                        (name.to_string(), bytes)
                    }
                    MarketEvent::Ticker { symbol, ticker } => {
                        let name = feed.symbols().name(symbol);
                        let row = ticker_row_from_event(&venue_name, name, ticker, now_ns);
                        let bytes = serde_json::to_vec(&row).unwrap();
                        (name.to_string(), bytes)
                    }
                    _ => continue,
                };

                in_bytes.fetch_add(record_bytes.len() as u64, Ordering::Relaxed);
                let _ = segment_writer.write_record(&symbol_name, &record_bytes, now_ns);
            }
        }
    }

    Ok(())
}
