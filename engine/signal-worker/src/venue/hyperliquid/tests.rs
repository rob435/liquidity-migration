use super::*;
use crate::normalize::{normalize_instruments_reporting, normalize_tickers_reporting};
use crate::venue::PublicVenue;
use serde_json::json;
use std::sync::Arc;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::{mpsc, Semaphore};
use tokio::task::JoinHandle;

/// Recorded from `https://api.hyperliquid.xyz/info` on 2026-09-09.
const META: &str = include_str!("../../../tests/fixtures/hyperliquid/meta.json");
const META_AND_ASSET_CTXS: &str =
    include_str!("../../../tests/fixtures/hyperliquid/meta_and_asset_ctxs.json");
const CANDLES_BTC_1H: &str =
    include_str!("../../../tests/fixtures/hyperliquid/candles_btc_1h.json");
const CANDLES_KPEPE_1H: &str =
    include_str!("../../../tests/fixtures/hyperliquid/candles_kpepe_1h.json");
const FUNDING_BTC: &str = include_str!("../../../tests/fixtures/hyperliquid/funding_btc.json");
const DAILY_BTC: &str = include_str!("../../../tests/fixtures/hyperliquid/daily_btc.json");
const DAILY_KPEPE: &str = include_str!("../../../tests/fixtures/hyperliquid/daily_kpepe.json");
const DAILY_HYPE: &str = include_str!("../../../tests/fixtures/hyperliquid/daily_hype.json");

fn fixture(text: &str) -> Value {
    serde_json::from_str(text).expect("recorded fixture is JSON")
}

fn universe_rows() -> Vec<Value> {
    meta_universe(&fixture(META)).unwrap().clone()
}

/// One local stand-in venue answering `/info` from the request body's `type`,
/// with every request body it was sent.
async fn info_source(
    respond: impl Fn(&Value) -> Value + Send + Sync + 'static,
) -> (
    HyperliquidPublicVenue,
    mpsc::UnboundedReceiver<Value>,
    JoinHandle<()>,
) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let (requests, received) = mpsc::unbounded_channel();
    let server = tokio::spawn(async move {
        loop {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut head = Vec::new();
            while !head.ends_with(b"\r\n\r\n") {
                let byte = socket.read_u8().await.unwrap();
                head.push(byte);
            }
            let head = String::from_utf8(head).unwrap();
            let length: usize = head
                .lines()
                .find_map(|line| {
                    line.strip_prefix("content-length: ")
                        .or_else(|| line.strip_prefix("Content-Length: "))
                })
                .map_or(0, |value| value.trim().parse().unwrap());
            let mut body = vec![0_u8; length];
            socket.read_exact(&mut body).await.unwrap();
            let body: Value = serde_json::from_slice(&body).unwrap();
            requests.send(body.clone()).unwrap();
            let payload = respond(&body).to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
                payload.len()
            );
            socket.write_all(response.as_bytes()).await.unwrap();
            socket.shutdown().await.unwrap();
        }
    });
    let client = PublicHttpClient::for_http_test(base, Arc::new(Semaphore::new(2)));
    (
        HyperliquidPublicVenue::for_http_test(client),
        received,
        server,
    )
}

/// Every coin in the venue's own universe, with the two spellings that differ
/// and the delisted rows that keep their place in it.
#[test]
fn every_meta_row_becomes_one_instrument_row_in_the_engines_spelling() {
    let rows = universe_rows();
    assert_eq!(rows.len(), 234);
    let launch = BTreeMap::from([("kPEPE".to_owned(), 1_683_244_800_000_i64)]);
    let wires = rows
        .iter()
        .map(|row| instrument_wire(row, &launch))
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    let by_symbol = wires
        .iter()
        .map(|row| (row.symbol.as_str(), row))
        .collect::<BTreeMap<_, _>>();
    assert_eq!(by_symbol.len(), wires.len(), "every symbol is its own");

    let btc = by_symbol["BTCUSDT"];
    assert_eq!(btc.contract_type.as_deref(), Some("LinearPerpetual"));
    assert_eq!(btc.symbol_type, None);
    assert_eq!(btc.status.as_deref(), Some("Trading"));
    assert_eq!(btc.base_coin.as_deref(), Some("BTC"));
    assert_eq!(btc.quote_coin.as_deref(), Some("USD"));
    assert_eq!(btc.settle_coin.as_deref(), Some("USDC"));
    assert_eq!(btc.funding_interval, Some(Value::from(60)));
    assert_eq!(btc.delivery_time, None);
    assert!(!btc.is_pre_listing);
    // szDecimals 5: sizes carry five decimals and prices carry one.
    assert_eq!(btc.price_filter["tickSize"], Value::from("0.1"));
    assert_eq!(btc.lot_size_filter["qtyStep"], Value::from("0.00001"));
    assert_eq!(btc.lot_size_filter["minOrderQty"], Value::from("0.00001"));
    assert_eq!(btc.lot_size_filter["minNotionalValue"], Value::from(10));
    assert!(!btc.lot_size_filter.contains_key("maxOrderQty"));
    assert!(!btc.lot_size_filter.contains_key("maxMktOrderQty"));
    assert_eq!(
        btc.launch_time, None,
        "only the read coin has a launch time"
    );

    // `kPEPE` is `KPEPEUSDT` to the engine and keeps the venue's own spelling
    // as its base coin.
    let kpepe = by_symbol["KPEPEUSDT"];
    assert_eq!(kpepe.base_coin.as_deref(), Some("kPEPE"));
    assert_eq!(kpepe.launch_time, Some(Value::from(1_683_244_800_000_i64)));
    // szDecimals 0: whole units, and six decimals of price.
    let doge = by_symbol["kDOGSUSDT".to_ascii_uppercase().as_str()];
    assert_eq!(doge.lot_size_filter["qtyStep"], Value::from("1"));
    assert_eq!(doge.price_filter["tickSize"], Value::from("0.000001"));

    assert_eq!(by_symbol["MATICUSDT"].status.as_deref(), Some("Closed"));
    let closed = wires
        .iter()
        .filter(|row| row.status.as_deref() == Some("Closed"))
        .count();
    assert_eq!(closed, 56);

    // Not one recorded row is left out of the table.
    let (normalized, rejected) = normalize_instruments_reporting(1, 2, &wires).unwrap();
    assert_eq!(normalized.len(), wires.len());
    assert_eq!(rejected.rows, Vec::new());
    assert!(normalized
        .iter()
        .all(|row| row.settle_coin.as_deref() == Some("USDC")
            && row.funding_interval_min == Some(60)
            && row.delivery_time_ms.is_none()
            && row.min_notional_value == Some(10.0)));

    let nameless = instrument_wire(&json!({"szDecimals": 2}), &launch).unwrap_err();
    assert!(nameless.to_string().contains("lacks name"), "{nameless}");
    let deep = instrument_wire(&json!({"name": "X", "szDecimals": 9}), &launch).unwrap_err();
    assert!(deep.to_string().contains("szDecimals"), "{deep}");
}

/// The venue pairs a context to a coin by index and nothing else, so every
/// index is checked, delisted rows included.
#[test]
fn the_ticker_page_pairs_every_context_with_its_own_coin() {
    let payload = fixture(META_AND_ASSET_CTXS);
    let (universe, contexts) = meta_and_asset_contexts(&payload).unwrap();
    assert_eq!(universe.len(), 234);
    let next_funding_time_ms = 4 * HOUR_MS;
    let rows = universe
        .iter()
        .zip(contexts)
        .map(|(row, context)| {
            let (symbol, _) = coin_names(row).unwrap();
            ticker_wire(symbol, context, next_funding_time_ms)
        })
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    for (index, row) in rows.iter().enumerate() {
        assert_eq!(
            row.symbol,
            engine_symbol(universe[index]["name"].as_str().unwrap()),
            "row {index} carries another coin's name"
        );
        assert_eq!(
            row.mark_price,
            stated(&contexts[index], "markPx"),
            "row {index} carries another coin's mark"
        );
        assert_eq!(row.bid1_price, None);
        assert_eq!(row.ask1_price, None);
        assert_eq!(row.bid1_size, None);
        assert_eq!(row.ask1_size, None);
        assert_eq!(
            row.next_funding_time,
            Some(Value::from(next_funding_time_ms))
        );
    }
    let by_symbol = rows
        .iter()
        .map(|row| (row.symbol.as_str(), row))
        .collect::<BTreeMap<_, _>>();
    let btc = by_symbol["BTCUSDT"];
    assert_eq!(btc.last_price, Some(Value::from("79020.5")));
    assert_eq!(btc.mark_price, Some(Value::from("79020.0")));
    assert_eq!(btc.index_price, Some(Value::from("79055.0")));
    assert_eq!(btc.turnover24h, Some(Value::from("2192654062.7202749252")));
    assert_eq!(btc.volume24h, Some(Value::from("27873.32314")));
    assert_eq!(btc.open_interest, Some(Value::from("34939.3239")));
    assert_eq!(btc.funding_rate, Some(Value::from("0.0000125")));
    let notional = 34939.3239_f64 * 79020.0_f64;
    assert_eq!(
        btc.open_interest_value,
        Some(json_number(notional, "").unwrap())
    );

    // A delisted coin has a mark and no mid, so it keeps a usable row without
    // a last price.
    let matic = by_symbol["MATICUSDT"];
    assert_eq!(matic.last_price, None);
    assert_eq!(matic.mark_price, Some(Value::from("0.37621")));
    assert_eq!(
        matic.open_interest_value,
        Some(json_number(0.0, "").unwrap())
    );

    let (normalized, rejected) = normalize_tickers_reporting(1, 2, &rows).unwrap();
    assert_eq!(normalized.len(), rows.len());
    assert_eq!(rejected.rows, Vec::new());

    let short = json!([{"universe": [{"name": "BTC", "szDecimals": 5}]}, []]);
    let error = meta_and_asset_contexts(&short).unwrap_err();
    assert!(error.to_string().contains("do not line up"), "{error}");
}

/// The venue publishes no quote turnover per bar, so it is derived and said to
/// be derived.
#[test]
fn the_derived_turnover_is_the_base_volume_times_the_mean_price() {
    let bars = fixture(CANDLES_BTC_1H);
    let rows = candle_list(&bars)
        .unwrap()
        .iter()
        .map(kline_row)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(rows.len(), 24);
    let first = &bars.as_array().unwrap()[0];
    assert_eq!(rows[0][0], Value::from(first["t"].as_i64().unwrap()));
    for (index, key) in ["o", "h", "l", "c", "v"].iter().enumerate() {
        assert_eq!(rows[0][index + 1], first[key], "{key} is the venue's own");
    }
    let mean = ["o", "h", "l", "c"]
        .iter()
        .map(|key| first[key].as_str().unwrap().parse::<f64>().unwrap())
        .sum::<f64>()
        / 4.0;
    let volume = first["v"].as_str().unwrap().parse::<f64>().unwrap();
    assert_eq!(rows[0][6], json_number(volume * mean, "").unwrap());

    // Closed bars an hour apart, and every one of them a valid bar.
    let opens = rows
        .iter()
        .map(|row| row[0].as_i64().unwrap())
        .collect::<Vec<_>>();
    assert!(opens.windows(2).all(|pair| pair[1] - pair[0] == HOUR_MS));
    assert!(opens.iter().all(|open| open % HOUR_MS == 0));
    let available = opens.last().unwrap() + HOUR_MS;
    let normalized = normalize_kline_rows("BTCUSDT", available, &rows).unwrap();
    assert_eq!(normalized.len(), 24);
    assert!(normalized.iter().all(|bar| bar.turnover_quote > 0.0));

    let kpepe = fixture(CANDLES_KPEPE_1H);
    let kpepe_rows = candle_list(&kpepe)
        .unwrap()
        .iter()
        .map(kline_row)
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    normalize_kline_rows("KPEPEUSDT", available, &kpepe_rows).unwrap();

    let short = kline_row(&json!({"t": HOUR_MS, "o": "1", "h": "1", "l": "1"})).unwrap_err();
    assert!(short.to_string().contains("lacks c"), "{short}");
}

/// The venue stamps a print a few milliseconds after the hour it settled; the
/// grid the worker keeps is the hour itself.
#[test]
fn a_funding_print_is_floored_to_the_hour_it_settled() {
    let payload = fixture(FUNDING_BTC);
    let rows = funding_list(&payload).unwrap();
    assert_eq!(rows.len(), 24);
    let printed = rows
        .iter()
        .map(|row| row["time"].as_i64().unwrap())
        .collect::<Vec<_>>();
    assert!(
        printed.iter().any(|time| time % HOUR_MS != 0),
        "the recorded prints land after their hour"
    );
    let settled = printed
        .iter()
        .map(|time| time - time.rem_euclid(HOUR_MS))
        .collect::<Vec<_>>();
    assert!(settled.iter().all(|time| time % HOUR_MS == 0));
    assert!(settled.windows(2).all(|pair| pair[1] - pair[0] == HOUR_MS));
    let wires = settled
        .iter()
        .zip(rows)
        .map(|(settlement_ms, row)| BybitFundingWire {
            funding_rate_timestamp: Value::from(*settlement_ms),
            funding_rate: row["fundingRate"].clone(),
            funding_interval_hour: Some(Value::from(1)),
        })
        .collect::<Vec<_>>();
    let available = settled.last().unwrap() + HOUR_MS;
    let normalized = normalize_funding_rows("BTCUSDT", available, &wires).unwrap();
    assert_eq!(normalized.len(), 24);
    // The venue's hourly rate reaches the reducers as the venue states it.
    assert!(normalized.iter().all(|row| row.funding_interval_min == 60));
    assert_eq!(
        normalized[0].rate,
        rows[0]["fundingRate"]
            .as_str()
            .unwrap()
            .parse::<f64>()
            .unwrap()
    );
}

/// A process that restored its universe from the checkpoint has read no `meta`
/// yet; the first lane read fills the coin table itself, once.
#[tokio::test(start_paused = true)]
async fn the_first_lane_read_fills_the_coin_table_from_meta() {
    let _io = crate::test_io::IoProgress::new();
    let (venue, mut requests, server) = info_source(|body| match body["type"].as_str() {
        Some("meta") => json!({"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}),
        Some("candleSnapshot") | Some("fundingHistory") => Value::from(Vec::<Value>::new()),
        other => panic!("unexpected request {other:?}"),
    })
    .await;
    venue.klines("BTCUSDT", 0, HOUR_MS, 10).await.unwrap();
    assert_eq!(requests.recv().await.unwrap()["type"], "meta");
    let candles = requests.recv().await.unwrap();
    assert_eq!(candles["type"], "candleSnapshot");
    assert_eq!(candles["req"]["coin"], "BTC");
    venue
        .funding("BTCUSDT", 0, HOUR_MS, 10, Some(1))
        .await
        .unwrap();
    while let Ok(body) = requests.try_recv() {
        assert_ne!(body["type"], "meta", "the table is read once");
    }
    server.abort();
}

/// Launch times the checkpoint already carries are not read again on boot.
#[tokio::test(start_paused = true)]
async fn a_seeded_listing_history_is_not_read_again() {
    let _io = crate::test_io::IoProgress::new();
    let (venue, mut requests, server) = info_source(|body| match body["type"].as_str() {
        Some("meta") => json!({"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}),
        other => panic!("unexpected request {other:?}"),
    })
    .await;
    venue.seed_listing_history(BTreeMap::from([(
        "BTCUSDT".to_owned(),
        1_597_795_200_000_i64,
    )]));
    let fetched = venue.instruments(1).await.unwrap();
    assert_eq!(
        fetched.rows[0].launch_time,
        Some(Value::from(1_597_795_200_000_i64))
    );
    tokio::task::yield_now().await;
    while let Ok(body) = requests.try_recv() {
        assert_eq!(body["type"], "meta", "a seeded coin is never read");
    }
    server.abort();
}

/// The listing history is read beside the instrument table, once a coin, and
/// never for a coin the venue delisted.
#[tokio::test(start_paused = true)]
async fn the_listing_history_is_read_once_a_coin_beside_the_table() {
    let _io = crate::test_io::IoProgress::new();
    let (venue, mut requests, server) = info_source(|body| match body["type"].as_str() {
        Some("meta") => json!({"universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "kPEPE", "szDecimals": 0, "maxLeverage": 10},
            {"name": "MATIC", "szDecimals": 1, "maxLeverage": 20, "isDelisted": true},
        ]}),
        Some("candleSnapshot") => match body["req"]["coin"].as_str() {
            Some("kPEPE") => fixture(DAILY_KPEPE),
            _ => fixture(DAILY_BTC),
        },
        other => panic!("unexpected request {other:?}"),
    })
    .await;

    // The first table is published without holding it back for a pass that
    // takes one read a coin: a name whose launch time is not in yet fails the
    // age gate and enters at a later refresh.
    let first = venue.instruments(1).await.unwrap();
    assert_eq!(first.rows.len(), 3);
    assert!(first.rows.iter().all(|row| row.launch_time.is_none()));
    assert_eq!(requests.recv().await.unwrap()["type"], "meta");

    // The reader runs beside this task; refreshes make room for it.
    let mut fetched = None;
    for _ in 0..64 {
        let rows = venue.instruments(1).await.unwrap();
        if rows
            .rows
            .iter()
            .filter(|row| row.launch_time.is_some())
            .count()
            == 2
        {
            fetched = Some(rows);
            break;
        }
        tokio::task::yield_now().await;
    }
    let fetched = fetched.expect("the listing history pass finishes");
    let mut read_coins = Vec::new();
    while let Ok(body) = requests.try_recv() {
        if body["type"] == "candleSnapshot" {
            assert_eq!(body["req"]["interval"], "1d");
            assert_eq!(body["req"]["startTime"], 0);
            read_coins.push(body["req"]["coin"].as_str().unwrap().to_owned());
        }
    }
    read_coins.sort();
    assert_eq!(
        read_coins,
        ["BTC", "kPEPE"],
        "a delisted coin is never read"
    );

    let by_symbol = fetched
        .rows
        .iter()
        .map(|row| (row.symbol.as_str(), row))
        .collect::<BTreeMap<_, _>>();
    assert_eq!(
        by_symbol["BTCUSDT"].launch_time,
        Some(Value::from(1_597_795_200_000_i64))
    );
    assert_eq!(
        by_symbol["KPEPEUSDT"].launch_time,
        Some(Value::from(1_683_244_800_000_i64))
    );
    assert_eq!(by_symbol["MATICUSDT"].launch_time, None);

    // A coin already read is never read again.
    venue.instruments(1).await.unwrap();
    while let Ok(body) = requests.try_recv() {
        assert_eq!(body["type"], "meta", "only the universe is read again");
    }
    server.abort();
}

/// A window never outruns what the venue answers with, whatever page limit the
/// realm's config carries: a longer one would leave the bars past the venue's
/// own row cap unread and still move the cursor past them.
#[tokio::test(start_paused = true)]
async fn a_window_is_bounded_by_the_venues_own_row_cap() {
    let _io = crate::test_io::IoProgress::new();
    let start = 0;
    let end = 6_000 * HOUR_MS;
    let (venue, mut requests, server) = info_source(|_| Value::from(Vec::<Value>::new())).await;
    venue.remember_coins(BTreeMap::from([("BTCUSDT".to_owned(), "BTC".to_owned())]));
    let (rows, _) = venue.klines("BTCUSDT", start, end, 10_000).await.unwrap();
    assert!(rows.is_empty());
    let first = requests.recv().await.unwrap();
    assert_eq!(
        first["req"]["endTime"],
        Value::from((MAX_CANDLE_ROWS as i64 - 1) * HOUR_MS)
    );
    let second = requests.recv().await.unwrap();
    assert_eq!(
        second["req"]["startTime"],
        Value::from(MAX_CANDLE_ROWS as i64 * HOUR_MS)
    );
    assert_eq!(second["req"]["endTime"], Value::from(end - HOUR_MS));

    let (funding, _) = venue
        .funding("BTCUSDT", start, end, 10_000, Some(1))
        .await
        .unwrap();
    assert!(funding.is_empty());
    let first = requests.recv().await.unwrap();
    assert_eq!(
        first["endTime"],
        Value::from(MAX_FUNDING_ROWS as i64 * HOUR_MS - 1)
    );
    let second = requests.recv().await.unwrap();
    assert_eq!(
        second["startTime"],
        Value::from(MAX_FUNDING_ROWS as i64 * HOUR_MS)
    );
    server.abort();
}

/// A window asks for bars that have closed inside it, and a print that lands
/// after the last settlement's hour still belongs to that settlement.
#[tokio::test(start_paused = true)]
async fn every_window_asks_the_venue_for_the_grid_the_caller_asked_for() {
    let _io = crate::test_io::IoProgress::new();
    let start = 100 * HOUR_MS;
    let end = 124 * HOUR_MS;
    let (venue, mut requests, server) = info_source(move |body| match body["type"].as_str() {
        Some("candleSnapshot") => {
            let bars = (0..24)
                .map(|hour| {
                    json!({
                        "t": start + hour * HOUR_MS,
                        "T": start + (hour + 1) * HOUR_MS - 1,
                        "s": "BTC", "i": "1h",
                        "o": "100", "h": "110", "l": "90", "c": "105", "v": "2", "n": 7,
                    })
                })
                .collect::<Vec<_>>();
            Value::from(bars)
        }
        Some("fundingHistory") => Value::from(
            (0..24)
                .map(|hour| {
                    json!({
                        "coin": "BTC",
                        "fundingRate": "0.0000125",
                        "premium": "0.0",
                        "time": start + hour * HOUR_MS + 17,
                    })
                })
                .collect::<Vec<_>>(),
        ),
        other => panic!("unexpected request {other:?}"),
    })
    .await;
    venue.remember_coins(BTreeMap::from([("BTCUSDT".to_owned(), "BTC".to_owned())]));

    let (rows, available) = venue.klines("BTCUSDT", start, end, 1_000).await.unwrap();
    assert_eq!(rows.len(), 24);
    assert!(available >= start);
    let asked = requests.recv().await.unwrap();
    assert_eq!(asked["req"]["startTime"], Value::from(start));
    assert_eq!(
        asked["req"]["endTime"],
        Value::from(end - HOUR_MS),
        "the last bar a window may carry opens an hour before its end"
    );
    assert_eq!(asked["req"]["interval"], "1h");
    assert_eq!(asked["req"]["coin"], "BTC");

    let (funding, _) = venue
        .funding("BTCUSDT", start, end - HOUR_MS, 200, Some(1))
        .await
        .unwrap();
    assert_eq!(funding.len(), 24);
    assert_eq!(
        funding[0].funding_rate_timestamp,
        Value::from(start),
        "a print is filed under the hour it settled"
    );
    assert_eq!(funding[0].funding_interval_hour, Some(Value::from(1)));
    let asked = requests.recv().await.unwrap();
    assert_eq!(asked["startTime"], Value::from(start));
    assert_eq!(
        asked["endTime"],
        Value::from(end - 1),
        "the window reaches to just before the settlement after its last one"
    );
    server.abort();
}

/// A name the last universe read did not carry is that lane's failure, not the
/// worker's.
#[tokio::test(start_paused = true)]
async fn a_coin_the_venue_stopped_listing_fails_only_its_own_lane() {
    let _io = crate::test_io::IoProgress::new();
    let (venue, _requests, server) = info_source(|_| Value::from(Vec::<Value>::new())).await;
    // The table has been read; the name is simply not in it.
    venue.remember_coins(BTreeMap::from([("BTCUSDT".to_owned(), "BTC".to_owned())]));
    for error in [
        venue.klines("GONEUSDT", 0, HOUR_MS, 10).await.unwrap_err(),
        venue
            .funding("GONEUSDT", 0, HOUR_MS, 10, Some(1))
            .await
            .unwrap_err(),
    ] {
        assert!(error.to_string().contains("lists no coin"), "{error}");
        assert!(error.is_lane_local_source_failure(), "{error}");
    }
    server.abort();
}

/// Reads leave one interval between them, so a whole instrument pass stays
/// inside the weight the venue gives this IP.
#[tokio::test(start_paused = true)]
async fn the_request_pacer_holds_every_read_to_the_venues_interval() {
    let pacer = RequestPacer::new(Duration::from_millis(INFO_READ_INTERVAL_MS));
    let started = Instant::now();
    for _ in 0..4 {
        pacer.wait().await;
    }
    assert_eq!(
        started.elapsed(),
        Duration::from_millis(3 * INFO_READ_INTERVAL_MS)
    );
    let free = RequestPacer::new(Duration::ZERO);
    let started = Instant::now();
    free.wait().await;
    free.wait().await;
    assert_eq!(started.elapsed(), Duration::ZERO);
}

/// The realm's own files, read the way `check-config` reads them.
#[test]
fn the_hyperliquid_realm_files_name_this_venue_and_open_it() {
    let config = crate::config::tests::checked_realm_config("hyperliquid");
    assert_eq!(config.sources.public_venue, "hyperliquid");
    assert_eq!(config.live.environment, "hyperliquid");
    assert_eq!(config.live.public_market_realm, "mainnet");
    // With native instruments the venue's own table is the listing, so no
    // second venue bounds the domain.
    assert_eq!(config.universe.listed_on, None);
    // The settle coin the source contract states stays Bybit's; the coin the
    // domain checks is the venue's own.
    assert_eq!(config.sources.bybit_settle_coin, "USDT");
    let venue = crate::venue::open_public_venue(
        config.public_venue().unwrap(),
        &config,
        Arc::new(Semaphore::new(1)),
    )
    .unwrap();
    assert_eq!(venue.kind(), PublicVenueKind::Hyperliquid);
    assert_eq!(venue.settle_coin(), "USDC");
}

/// The earliest daily bar is the only listing clock the venue publishes. For a
/// coin listed after the exchange it is the listing week; for the majors the
/// venue carries history that predates the exchange, so the number is a floor
/// on the age rather than a listing date.
#[test]
fn the_earliest_daily_bar_is_the_listing_clock_the_venue_publishes() {
    let earliest = |text: &str| {
        candle_list(&fixture(text))
            .unwrap()
            .iter()
            .map(|row| row["t"].as_i64().unwrap())
            .min()
            .unwrap()
    };
    // HYPE listed in the week of 2024-12-05.
    assert_eq!(earliest(DAILY_HYPE), 1_733_356_800_000);
    assert_eq!(earliest(DAILY_KPEPE), 1_683_244_800_000);
    // BTC's first daily bar is 2020-08-19, years before the exchange.
    assert_eq!(earliest(DAILY_BTC), 1_597_795_200_000);
    assert!(earliest(DAILY_BTC) < earliest(DAILY_HYPE));
}

#[test]
fn the_next_settlement_is_the_next_whole_hour() {
    assert_eq!(next_settlement_ms(0), HOUR_MS);
    assert_eq!(next_settlement_ms(1), HOUR_MS);
    assert_eq!(next_settlement_ms(HOUR_MS), 2 * HOUR_MS);
    assert_eq!(next_settlement_ms(HOUR_MS - 1), HOUR_MS);
    for now in [1_i64, 999, HOUR_MS + 7, 1_788_944_400_123] {
        assert!(next_settlement_ms(now) > now);
        assert!(next_settlement_ms(now) - now <= HOUR_MS);
    }
}

#[test]
fn a_decimal_step_is_written_out_rather_than_rounded() {
    assert_eq!(decimal_step(0), "1");
    assert_eq!(decimal_step(1), "0.1");
    assert_eq!(decimal_step(6), "0.000001");
    for decimals in 0..=6 {
        let step: f64 = decimal_step(decimals).parse().unwrap();
        assert_eq!(step, 10_f64.powi(-i32::from(decimals as u8)));
    }
}

#[test]
fn the_coin_a_symbol_names_falls_back_to_the_upper_case_spelling() {
    assert_eq!(engine_symbol("BTC"), "BTCUSDT");
    assert_eq!(engine_symbol("kPEPE"), "KPEPEUSDT");
    assert_eq!(default_coin("BTCUSDT"), "BTC");
    assert_eq!(default_coin("KPEPEUSDT"), "KPEPE");
}
