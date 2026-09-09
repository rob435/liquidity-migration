use super::*;

use std::sync::Arc;

use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use crate::normalize::{normalize_instruments_reporting, normalize_tickers_reporting};

/// Recorded from the realm's own host on 2026-09-09. Twenty-seven real rows,
/// chosen to span every contract size the venue lists (1e-5 to 1e7), all four
/// settlement cycles, both order ceilings when they differ, a contract the
/// venue will not take API orders on, the three non-USDT quotes, and an
/// inverse contract. Real bytes, so a renamed field fails here.
const DETAIL: &str = include_str!("../../../tests/fixtures/mexc/contract_detail.json");
/// `contract/funding_rate` with no symbol: the whole venue's cycle and next
/// settlement clock in one request. Carries four contracts the contract list
/// does not, which is the live shape.
const FUNDING: &str = include_str!("../../../tests/fixtures/mexc/funding_rate.json");
const TICKER: &str = include_str!("../../../tests/fixtures/mexc/ticker.json");
/// `contract/kline` for a 0.0001-per-contract name and a 10000000-per-contract
/// one, 49 closed hourly bars each.
const KLINE_BTC: &str = include_str!("../../../tests/fixtures/mexc/kline_btc.json");
const KLINE_PEPE: &str = include_str!("../../../tests/fixtures/mexc/kline_pepe.json");
/// Three consecutive newest-first history pages for an eight-hourly contract,
/// and two for a four-hourly one.
const HISTORY_BTC: &str = include_str!("../../../tests/fixtures/mexc/funding_history_btc.json");
const HISTORY_FORM: &str = include_str!("../../../tests/fixtures/mexc/funding_history_form.json");

fn json(text: &str) -> Value {
    serde_json::from_str(text).expect("recorded fixture is JSON")
}

fn cycles() -> BTreeMap<String, (i64, i64)> {
    read_funding_cycles(mexc_data(&json(FUNDING), "funding rate").unwrap()).unwrap()
}

fn table_and_rows() -> (ContractTable, Vec<BybitInstrumentWire>) {
    read_contract_detail(
        mexc_data(&json(DETAIL), "contract detail").unwrap(),
        &cycles(),
    )
    .unwrap()
}

fn wire_of(rows: &[BybitInstrumentWire], symbol: &str) -> BybitInstrumentWire {
    rows.iter()
        .find(|row| row.symbol == symbol)
        .unwrap_or_else(|| panic!("{symbol} is not in the recorded page"))
        .clone()
}

fn lot(row: &BybitInstrumentWire, key: &str) -> Option<f64> {
    row.lot_size_filter
        .get(key)
        .and_then(|value| value_f64(value, key).ok())
}

/// The venue's spelling is not derivable from the engine's: `USD`, `USDT`,
/// `USDC` and `USD1` are all live quotes here. The mapping is read from the
/// row's own coins, the same rule `engine-public`'s contract table uses, and
/// this asserts the two agree on every row that table keeps.
#[test]
fn the_recorded_page_maps_both_spellings_the_way_the_engines_own_table_does() {
    let (table, rows) = table_and_rows();
    assert_eq!(table.len(), 27);
    assert_eq!(rows.len(), 27);
    assert_eq!(table.venue_symbol("BTCUSDT"), Some("BTC_USDT"));
    assert_eq!(table.engine_symbol("BTC_USDT"), Some("BTCUSDT"));
    assert_eq!(table.venue_symbol("BTCUSD"), Some("BTC_USD"));
    assert_eq!(table.venue_symbol("ZECUSD1"), Some("ZEC_USD1"));
    assert_eq!(table.engine_symbol("NOPE_USDT"), None);

    let engine_side = engine_public::venues::mexc::contracts::Contracts::parse_raw(DETAIL).unwrap();
    for (symbol, venue_symbol) in engine_side.symbol_pairs() {
        assert_eq!(
            table.venue_symbol(&symbol),
            Some(venue_symbol.as_str()),
            "{symbol} disagrees with the engine's own contract table"
        );
        assert_eq!(
            table.row(&symbol).unwrap().contract_size,
            engine_side.any(&symbol).unwrap().contract_size,
            "{symbol} contract size disagrees"
        );
    }
}

/// A page's rows come back in one order, twice. The catalog checkpoint and the
/// instrument journal compare rows as a sequence.
#[test]
fn two_reads_of_one_page_produce_the_same_rows_in_the_same_order() {
    let first = table_and_rows().1;
    let second = table_and_rows().1;
    assert_eq!(first, second);
    let symbols = first
        .iter()
        .map(|row| row.symbol.clone())
        .collect::<Vec<_>>();
    let mut sorted = symbols.clone();
    sorted.sort();
    assert_eq!(symbols, sorted, "instrument rows are not sorted by symbol");
}

/// The file that stops every quantity being wrong by its multiplier. MEXC
/// states order sizes in contracts; the wire is in base coin.
#[test]
fn quantities_convert_from_contracts_on_both_a_tiny_and_a_huge_multiplier() {
    let (table, rows) = table_and_rows();
    // 0.0001 BTC per contract; minVol 1, maxVol 400000, limitMaxVol 2500000.
    let btc = wire_of(&rows, "BTCUSDT");
    assert_eq!(table.row("BTCUSDT").unwrap().contract_size, 0.0001);
    assert_eq!(lot(&btc, "qtyStep"), Some(0.0001));
    assert_eq!(lot(&btc, "minOrderQty"), Some(0.0001));
    assert_eq!(lot(&btc, "maxOrderQty"), Some(250.0));
    assert_eq!(lot(&btc, "maxMktOrderQty"), Some(40.0));
    assert_eq!(
        btc.price_filter.get("tickSize"),
        Some(&Value::from(0.1)),
        "tickSize is priceUnit"
    );

    // 10000000 PEPE per contract.
    let pepe = wire_of(&rows, "PEPEUSDT");
    assert_eq!(table.row("PEPEUSDT").unwrap().contract_size, 10_000_000.0);
    assert_eq!(lot(&pepe, "qtyStep"), Some(10_000_000.0));
    assert_eq!(lot(&pepe, "minOrderQty"), Some(10_000_000.0));
    assert_eq!(lot(&pepe, "maxOrderQty"), Some(24_000.0 * 10_000_000.0));

    // The venue publishes two ceilings and they are not the same number:
    // `limitMaxVol` bounds a limit order and `maxVol` a market one. AGLD is
    // the widest gap on the venue.
    let agld = wire_of(&rows, "AGLDUSDT");
    assert_eq!(lot(&agld, "maxOrderQty"), Some(3_000_000_000.0));
    assert_eq!(lot(&agld, "maxMktOrderQty"), Some(30_000.0));

    // No minimum notional is claimed: the venue's minimum is in contracts and
    // is already in minOrderQty.
    assert!(!btc.lot_size_filter.contains_key("minNotionalValue"));
}

#[test]
fn api_trading_and_the_settlement_asset_decide_status_and_contract_type() {
    let (_, rows) = table_and_rows();
    let btc = wire_of(&rows, "BTCUSDT");
    assert_eq!(btc.status.as_deref(), Some("Trading"));
    assert_eq!(btc.contract_type.as_deref(), Some("LinearPerpetual"));
    assert_eq!(btc.settle_coin.as_deref(), Some("USDT"));
    assert_eq!(btc.base_coin.as_deref(), Some("BTC"));
    assert_eq!(btc.quote_coin.as_deref(), Some("USDT"));
    assert_eq!(btc.launch_time, Some(Value::from(1_591_242_684_000_i64)));
    assert!(
        btc.delivery_time.is_none(),
        "the venue lists perpetuals only"
    );
    assert!(!btc.is_pre_listing);
    // MEXC publishes no product-class label a reader can trust, so this stays
    // absent and reads as the venue's ordinary crypto product in universe.rs.
    assert!(btc.symbol_type.is_none());

    // The venue does set apiAllowed false, per contract.
    assert_eq!(wire_of(&rows, "ZZZUSDT").status.as_deref(), Some("Closed"));

    // A contract that settles in something other than its quote coin is not a
    // linear perpetual, and the universe domain drops it on that.
    let inverse = wire_of(&rows, "BTCUSD");
    assert_eq!(inverse.contract_type.as_deref(), Some("InversePerpetual"));
    assert_eq!(inverse.settle_coin.as_deref(), Some("BTC"));

    // Every row the venue publishes today carries state 0, so the not-normal
    // reading is asserted on a recorded row with its state moved.
    let mut halted = json(DETAIL);
    halted["data"][0]["state"] = Value::from(1);
    let (_, moved) =
        read_contract_detail(mexc_data(&halted, "contract detail").unwrap(), &cycles()).unwrap();
    let symbol = halted["data"][0]["baseCoin"].as_str().unwrap().to_owned()
        + halted["data"][0]["quoteCoin"].as_str().unwrap();
    assert_eq!(wire_of(&moved, &symbol).status.as_deref(), Some("Closed"));
}

/// `contract/detail` carries no settlement cycle at all, and the cycles are
/// not all the same: 1, 4, 8 and 24 hours are all live on this venue, so the
/// eight-hourly reading the market-data feed uses is wrong for about half of
/// it. The whole-venue funding page is where the cycle comes from.
#[test]
fn the_settlement_interval_comes_from_the_funding_page_per_contract() {
    let (_, rows) = table_and_rows();
    for (symbol, minutes) in [
        ("BTCUSDT", 480),
        ("FORMUSDT", 240),
        ("SOPHUSDT", 60),
        ("US30USDT", 1_440),
    ] {
        assert_eq!(
            wire_of(&rows, symbol).funding_interval,
            Some(Value::from(minutes)),
            "{symbol} settlement interval"
        );
    }

    // A contract the funding page does not price has no cycle and no clock,
    // and the wire leaves the field absent rather than assuming eight hours.
    let mut thinned = json(FUNDING);
    let kept = thinned["data"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["symbol"].as_str() != Some("BTC_USDT"))
        .cloned()
        .collect::<Vec<_>>();
    thinned["data"] = Value::Array(kept);
    let thinned_cycles = read_funding_cycles(mexc_data(&thinned, "funding rate").unwrap()).unwrap();
    let (table, rows) = read_contract_detail(
        mexc_data(&json(DETAIL), "contract detail").unwrap(),
        &thinned_cycles,
    )
    .unwrap();
    assert!(wire_of(&rows, "BTCUSDT").funding_interval.is_none());
    assert!(table.row("BTCUSDT").unwrap().cycle_hours.is_none());
    assert!(next_settlement_ms(table.row("BTCUSDT").unwrap(), 1).is_none());
}

/// The whole point of the instrument mapping: the recorded page has to produce
/// a universe. This runs the real derivation over the recorded instrument and
/// ticker pages, which is what checks status, settle coin, contract type,
/// symbol type, pre-listing and delivery clock all at once.
#[test]
fn the_recorded_pages_derive_a_universe_of_the_usdt_perpetuals() {
    let (table, instrument_rows) = table_and_rows();
    let observed = 1_788_946_500_000_i64;
    let (instruments, rejected) =
        normalize_instruments_reporting(observed, observed + 1, &instrument_rows).unwrap();
    assert_eq!(rejected.rows, Vec::new(), "no recorded row may be left out");
    let ticker_rows = mexc_list(mexc_data(&json(TICKER), "ticker").unwrap(), "ticker")
        .unwrap()
        .iter()
        .filter_map(|row| ticker_wire(&table, row, observed))
        .collect::<Vec<_>>();
    let (tickers, rejected) =
        normalize_tickers_reporting(observed, observed + 1, &ticker_rows).unwrap();
    assert_eq!(rejected.rows, Vec::new());

    let rules = crate::universe::UniverseRules {
        listed_on: None,
        exclude_symbols: vec!["USD1USDT".into()],
        long: crate::universe::SleeveUniverseRule {
            min_turnover_24h_usdt: 0.0,
            min_listing_age_days: 0,
            enter_rank: 5,
            leave_rank: 5,
        },
        carry: crate::universe::SleeveUniverseRule {
            min_turnover_24h_usdt: 0.0,
            min_listing_age_days: 0,
            enter_rank: 20,
            leave_rank: 20,
        },
    };
    let derived = crate::universe::derive_universe(
        &rules,
        crate::universe::UniverseInputs {
            environment: "mexc",
            endpoint: rest_host(),
            settle_coin: "USDT",
            snapshot_ts_ms: observed,
            available_at_ms: observed + 1,
            instruments: &instruments,
            tickers: &tickers,
            listing: None,
            previous: None,
        },
    )
    .unwrap();
    assert!(derived.symbols.contains(&"BTCUSDT".to_owned()));
    assert!(derived.symbols.contains(&"PEPEUSDT".to_owned()));
    // Not USDT-settled, not API-tradable, or excluded by name.
    for outside in ["BTCUSD", "BTCUSDC", "ZECUSD1", "ZZZUSDT"] {
        assert!(
            !derived.symbols.contains(&outside.to_owned()),
            "{outside} must not reach the universe"
        );
    }
    assert_eq!(derived.long_symbols.len(), 5);
    assert_eq!(derived.endpoint, "api.mexc.com");
}

#[test]
fn the_ticker_page_converts_contracts_and_states_no_size_or_open_interest_value() {
    let (table, _) = table_and_rows();
    let observed = 1_788_946_500_000_i64;
    let rows = mexc_list(mexc_data(&json(TICKER), "ticker").unwrap(), "ticker")
        .unwrap()
        .iter()
        .filter_map(|row| ticker_wire(&table, row, observed))
        .collect::<Vec<_>>();
    assert_eq!(rows.len(), 27, "the four unlisted contracts are left out");
    let recorded = mexc_list(mexc_data(&json(TICKER), "ticker").unwrap(), "ticker")
        .unwrap()
        .iter()
        .find(|row| row["symbol"].as_str() == Some("BTC_USDT"))
        .unwrap()
        .clone();
    let btc = rows.iter().find(|row| row.symbol == "BTCUSDT").unwrap();
    // A price the venue already states in the wire's unit goes through
    // verbatim, byte for byte.
    assert_eq!(btc.last_price.as_ref(), Some(&recorded["lastPrice"]));
    assert_eq!(
        btc.mark_price.as_ref(),
        Some(&recorded["fairPrice"]),
        "fairPrice is the mark"
    );
    assert_eq!(btc.index_price.as_ref(), Some(&recorded["indexPrice"]));
    assert_eq!(btc.bid1_price.as_ref(), Some(&recorded["bid1"]));
    assert_eq!(btc.ask1_price.as_ref(), Some(&recorded["ask1"]));
    assert_eq!(btc.turnover24h.as_ref(), Some(&recorded["amount24"]));
    assert_eq!(btc.funding_rate.as_ref(), Some(&recorded["fundingRate"]));
    // A quantity the venue states in contracts is converted, and the
    // conversion shaves float dust the way the engine's contract table does.
    assert_eq!(btc.volume24h, Some(Value::from(48_328.258_5)));
    assert_eq!(btc.open_interest, Some(Value::from(45_068.819_2)));
    // The ticker states a touch price and no size, and no open-interest
    // notional; nothing here multiplies two of our own numbers to invent one.
    assert!(btc.bid1_size.is_none() && btc.ask1_size.is_none());
    assert!(btc.open_interest_value.is_none());
    assert_eq!(
        btc.next_funding_time,
        Some(Value::from(1_788_969_600_000_i64))
    );
    // The venue prices contracts its own list omits; a row with no contract
    // size cannot be converted and is left out rather than guessed at 1.
    for unlisted in ["MXUSDT", "TONUSDT", "WBTCUSDT", "USDEUSDT"] {
        assert!(rows.iter().all(|row| row.symbol != unlisted), "{unlisted}");
    }
}

/// The ticker channel carries no settlement clock, so it is rolled forward
/// from the venue's own stamp by the venue's own cycle. It is not derived from
/// the epoch: `US30_USDT` settles daily and its next settlement is not on a
/// 24-hour boundary from the epoch, so an epoch grid is the wrong clock.
#[test]
fn the_settlement_clock_rolls_on_the_venues_phase_not_the_epochs() {
    let (table, _) = table_and_rows();
    const HOUR: i64 = HOUR_MS;
    let daily = table.row("US30USDT").unwrap();
    assert_eq!(daily.cycle_hours, Some(24));
    let stated = daily.next_settle_ms.unwrap();
    assert_eq!(stated, 1_788_969_600_000);
    assert_ne!(
        stated % (24 * HOUR),
        0,
        "the recorded daily contract is deliberately off the epoch's daily grid"
    );
    assert_eq!(next_settlement_ms(daily, stated - 1), Some(stated));
    assert_eq!(next_settlement_ms(daily, stated), Some(stated + 24 * HOUR));
    assert_eq!(
        next_settlement_ms(daily, stated + 25 * HOUR),
        Some(stated + 48 * HOUR)
    );

    let four_hourly = table.row("FORMUSDT").unwrap();
    assert_eq!(four_hourly.cycle_hours, Some(4));
    let stated = four_hourly.next_settle_ms.unwrap();
    assert_eq!(
        next_settlement_ms(four_hourly, stated),
        Some(stated + 4 * HOUR)
    );
    for now in [stated, stated + HOUR, stated + 3 * HOUR] {
        let next = next_settlement_ms(four_hourly, now).unwrap();
        assert!(next > now && next % HOUR == 0);
    }
}

#[test]
fn the_kline_columns_become_wire_rows_in_milliseconds_from_the_traded_bar() {
    let payload = json(KLINE_BTC);
    let data = mexc_data(&payload, "kline").unwrap();
    let rows = read_kline_columns(data, 0.0001).unwrap();
    assert_eq!(rows.len(), 49);
    let (first_ts, first) = rows[0].clone();
    assert_eq!(first_ts, 1_788_681_600_000);
    assert_eq!(first[0], Value::from(1_788_681_600_000_i64));
    assert_eq!(first[1], data["realOpen"][0]);
    assert_eq!(first[2], data["realHigh"][0]);
    assert_eq!(first[3], data["realLow"][0]);
    assert_eq!(first[4], data["realClose"][0]);
    assert_eq!(first[6], data["amount"][0]);
    // Contracts to base coin, through the shave the engine's contract table
    // uses: the raw product carries dust the wire must not.
    let contracts = value_f64(&data["vol"][0], "vol").unwrap();
    assert_eq!(
        first[5],
        Value::from(round_clean(contracts * 0.0001, 0.0001))
    );
    assert_ne!(first[5], Value::from(contracts * 0.0001));

    // The `open/high/low/close` columns are the stitched series whose open is
    // the previous bar's close, and they are not the traded bar. At least one
    // recorded bar proves the two differ, so a swap here would fail.
    assert!(
        (0..rows.len()).any(|index| data["open"][index] != data["realOpen"][index]),
        "the recorded page must contain a stitched open that differs"
    );

    // Every row passes the wire's own bar contract once the hour has elapsed.
    let last_open = rows.last().unwrap().0;
    let wire = rows.iter().map(|(_, row)| row.clone()).collect::<Vec<_>>();
    normalize_kline_rows("BTCUSDT", last_open + HOUR_MS, &wire).unwrap();
    assert!(normalize_kline_rows("BTCUSDT", last_open, &wire).is_err());

    // The same page on a 10000000-per-contract name.
    let pepe = json(KLINE_PEPE);
    let data = mexc_data(&pepe, "kline").unwrap();
    let rows = read_kline_columns(data, 10_000_000.0).unwrap();
    let contracts = value_f64(&data["vol"][0], "vol").unwrap();
    assert_eq!(
        rows[0].1[5],
        Value::from(round_clean(contracts * 10_000_000.0, 10_000_000.0))
    );
    let wire = rows.iter().map(|(_, row)| row.clone()).collect::<Vec<_>>();
    normalize_kline_rows("PEPEUSDT", rows.last().unwrap().0 + HOUR_MS, &wire).unwrap();
}

#[test]
fn a_column_the_venue_did_not_send_or_sent_short_fails_the_read() {
    let mut payload = json(KLINE_BTC);
    payload["data"]["realOpen"] = Value::Array(Vec::new());
    let error = read_kline_columns(mexc_data(&payload, "kline").unwrap(), 0.0001).unwrap_err();
    assert!(error.to_string().contains("different lengths"), "{error}");

    let mut payload = json(KLINE_BTC);
    payload["data"].as_object_mut().unwrap().remove("realClose");
    let error = read_kline_columns(mexc_data(&payload, "kline").unwrap(), 0.0001).unwrap_err();
    assert!(error.to_string().contains("realClose column"), "{error}");
}

/// The venue's rate limit is an HTTP 200 carrying `code 510`, so the status
/// alone never reveals it. It reads as a retryable lane-local failure, which
/// is the same reading the engine's own MEXC adapter gives it.
#[test]
fn the_rate_limit_envelope_is_a_retryable_network_failure() {
    let limited = mexc_data(
        &serde_json::json!({"success": false, "code": 510, "message": "Requests are too frequent"}),
        "ticker",
    )
    .unwrap_err();
    assert!(limited.is_lane_local_source_failure());
    assert!(limited.to_string().contains("510"), "{limited}");
    assert!(limited.to_string().contains("rate limited"), "{limited}");

    // Every other refusal is retryable too, the same as a non-zero Bybit
    // retCode, and names the code it arrived with.
    let refused = mexc_data(
        &serde_json::json!({"success": false, "code": 600, "message": "Parameter error"}),
        "kline",
    )
    .unwrap_err();
    assert!(refused.is_lane_local_source_failure());
    assert!(refused.to_string().contains("600"), "{refused}");
    assert!(mexc_data(&serde_json::json!({"code": 0}), "kline")
        .unwrap_err()
        .to_string()
        .contains("code=0"));
    assert!(
        mexc_data(&serde_json::json!({"success": true, "code": 0}), "kline")
            .unwrap_err()
            .to_string()
            .contains("lacks data")
    );
    assert_eq!(
        mexc_data(
            &serde_json::json!({"success": true, "code": 0, "data": [1]}),
            "kline"
        )
        .unwrap(),
        &serde_json::json!([1])
    );
}

#[test]
fn the_rest_host_is_the_realm_tables_host_without_a_scheme() {
    assert_eq!(rest_host(), "api.mexc.com");
    assert!(!rest_host().contains("://") && !rest_host().contains('/'));
    assert_eq!(
        format!("https://{}", rest_host()),
        engine_public::MexcRealm::Mainnet.rest_base()
    );
}

/// The realm's checked-in files, loaded through the real loader, name this
/// venue and open it.
#[test]
fn the_mexc_realm_files_name_this_venue_and_open_it() {
    let config = crate::config::tests::checked_realm_config("mexc");
    assert_eq!(config.sources.public_venue, "mexc");
    assert_eq!(config.public_venue().unwrap(), PublicVenueKind::Mexc);
    let venue = crate::venue::open_public_venue(
        config.public_venue().unwrap(),
        &config,
        Arc::new(Semaphore::new(1)),
    )
    .unwrap();
    assert_eq!(venue.kind(), PublicVenueKind::Mexc);
    // The instrument table is the listing here, so the realm names no second
    // listing venue to intersect with.
    assert_eq!(config.universe.listed_on, None);
    // A universe snapshot names the host its listings came from, and a Bybit
    // realm's stays where it was.
    assert_eq!(config.universe_endpoint(), rest_host());
    for realm in ["demo", "mainnet"] {
        let bybit = crate::config::tests::checked_realm_config(realm);
        assert_eq!(
            bybit.universe_endpoint(),
            crate::worker::realm_endpoint(&bybit)
        );
    }
}

async fn http_source(
    respond: impl Fn(&str) -> Value + Send + Sync + 'static,
) -> (
    PublicHttpClient,
    mpsc::UnboundedReceiver<String>,
    JoinHandle<()>,
) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let (requests, received) = mpsc::unbounded_channel();
    let server = tokio::spawn(async move {
        loop {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = Vec::new();
            while !request.ends_with(b"\r\n\r\n") {
                let byte = socket.read_u8().await.unwrap();
                request.push(byte);
            }
            let request = String::from_utf8(request).unwrap();
            let target = request.split_whitespace().nth(1).unwrap().to_owned();
            requests.send(target.clone()).unwrap();
            let body = respond(&target).to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            socket.write_all(response.as_bytes()).await.unwrap();
            socket.shutdown().await.unwrap();
        }
    });
    let client = PublicHttpClient::for_http_test(base, Arc::new(Semaphore::new(2)));
    (client, received, server)
}

fn query_number(target: &str, key: &str) -> i64 {
    target
        .split(['?', '&'])
        .find_map(|pair| pair.strip_prefix(&format!("{key}=")))
        .unwrap_or_else(|| panic!("{target} carries no {key}"))
        .parse()
        .unwrap()
}

/// The recorded venue: `contract/detail` and the whole funding page whole,
/// klines sliced to the requested window the way the venue slices them, and
/// funding history by page number.
fn recorded_venue(target: &str) -> Value {
    if target.starts_with(PATH_DETAIL) {
        return json(DETAIL);
    }
    if target.starts_with(PATH_FUNDING_HISTORY) {
        let page = usize::try_from(query_number(target, "page_num")).unwrap();
        let pages: Vec<Value> = serde_json::from_str(if target.contains("FORM") {
            HISTORY_FORM
        } else {
            HISTORY_BTC
        })
        .unwrap();
        return pages.get(page - 1).cloned().unwrap_or_else(|| {
            json(r#"{"success":true,"code":0,"data":{"resultList":[],"totalPage":0}}"#)
        });
    }
    if target.starts_with(PATH_FUNDING) {
        return json(FUNDING);
    }
    if target.starts_with(PATH_KLINE) {
        let (start, end) = (query_number(target, "start"), query_number(target, "end"));
        let mut payload = json(if target.contains("PEPE") {
            KLINE_PEPE
        } else {
            KLINE_BTC
        });
        let data = payload["data"].clone();
        let kept = data["time"]
            .as_array()
            .unwrap()
            .iter()
            .enumerate()
            // The venue's window is in seconds and its end is inclusive.
            .filter(|(_, ts)| {
                let ts = ts.as_i64().unwrap();
                start <= ts && ts <= end
            })
            .map(|(index, _)| index)
            .collect::<Vec<_>>();
        for column in [
            "time",
            "open",
            "close",
            "high",
            "low",
            "vol",
            "amount",
            "realOpen",
            "realClose",
            "realHigh",
            "realLow",
        ] {
            let values = data[column].as_array().unwrap();
            payload["data"][column] =
                Value::Array(kept.iter().map(|index| values[*index].clone()).collect());
        }
        return payload;
    }
    json(TICKER)
}

#[tokio::test(start_paused = true)]
async fn the_kline_grid_is_walked_in_windows_of_the_page_limit_with_an_exclusive_end() {
    let _io = crate::test_io::IoProgress::new();
    let (client, mut requests, server) = http_source(recorded_venue).await;
    let venue = MexcPublicVenue::for_http_test(client);
    let start = 1_788_681_600_000_i64;
    let end = 1_788_854_400_000_i64 + HOUR_MS;
    let (rows, available) = venue.fetch_klines("BTCUSDT", start, end, 10).await.unwrap();
    assert_eq!(rows.len(), 49);
    assert_eq!(rows[0][0], Value::from(start));
    assert_eq!(rows[48][0], Value::from(end - HOUR_MS));
    assert!(available >= start);

    // The two table reads, then one kline request per ten-hour window.
    let targets = std::iter::from_fn(|| requests.try_recv().ok()).collect::<Vec<_>>();
    assert!(targets[0].starts_with(PATH_DETAIL), "{:?}", targets[0]);
    assert!(targets[1].starts_with(PATH_FUNDING), "{:?}", targets[1]);
    let windows = &targets[2..];
    assert_eq!(windows.len(), 5);
    assert!(windows
        .iter()
        .all(|target| target.contains("interval=Min60")
            && target.starts_with("/api/v1/contract/kline/BTC_USDT")));
    assert_eq!(query_number(&windows[0], "start"), start / 1_000);
    assert_eq!(query_number(&windows[0], "end"), start / 1_000 + 9 * 3_600);
    // The wire's end is exclusive and the venue's is inclusive, so the last
    // hour asked for is one hour short of it.
    assert_eq!(query_number(&windows[4], "end"), (end - HOUR_MS) / 1_000);
    server.abort();
}

#[tokio::test(start_paused = true)]
async fn a_bar_outside_the_requested_grid_fails_the_kline_read() {
    let _io = crate::test_io::IoProgress::new();
    let (client, _requests, server) = http_source(|target: &str| {
        let mut payload = recorded_venue(target);
        if target.starts_with(PATH_KLINE) {
            // One bar half an hour off the grid.
            if let Some(first) = payload["data"]["time"].get_mut(0) {
                *first = Value::from(1_788_681_600_i64 + 1_800);
            }
        }
        payload
    })
    .await;
    let venue = MexcPublicVenue::for_http_test(client);
    let error = venue
        .fetch_klines(
            "BTCUSDT",
            1_788_681_600_000,
            1_788_854_400_000 + HOUR_MS,
            60,
        )
        .await
        .unwrap_err();
    assert!(
        error
            .to_string()
            .contains("outside the requested source grid"),
        "{error}"
    );
    server.abort();
}

#[tokio::test(start_paused = true)]
async fn funding_pages_backwards_and_answers_ascending_with_the_venues_own_cycle() {
    let _io = crate::test_io::IoProgress::new();
    let (client, mut requests, server) = http_source(recorded_venue).await;
    let venue = MexcPublicVenue::for_http_test(client);
    // Page 1 of the recording reaches back to 1788393600000 and page 2 to
    // 1787817600000, so this window stops the walk on page 2.
    let start = 1_787_817_600_000_i64;
    let end = 1_788_940_800_000_i64;
    let (rows, available) = venue
        .fetch_funding("BTCUSDT", start, end, 20, Some(8))
        .await
        .unwrap();
    assert_eq!(rows.len(), 40);
    let stamps = rows
        .iter()
        .map(|row| value_i64(&row.funding_rate_timestamp, "settlement").unwrap())
        .collect::<Vec<_>>();
    let mut ascending = stamps.clone();
    ascending.sort_unstable();
    assert_eq!(stamps, ascending, "settlements are not ascending");
    assert_eq!(*stamps.first().unwrap(), start);
    assert_eq!(*stamps.last().unwrap(), end);
    assert!(stamps
        .iter()
        .all(|stamp| stamp.rem_euclid(HOUR_MS) == 0 && stamp.rem_euclid(8 * HOUR_MS) == 0));
    assert!(rows
        .iter()
        .all(|row| row.funding_interval_hour == Some(Value::from(8))));
    crate::normalize::normalize_funding_rows("BTCUSDT", available, &rows).unwrap();

    let targets = std::iter::from_fn(|| requests.try_recv().ok()).collect::<Vec<_>>();
    let pages = targets
        .iter()
        .filter(|target| target.starts_with(PATH_FUNDING_HISTORY))
        .collect::<Vec<_>>();
    assert_eq!(pages.len(), 2, "{pages:?}");
    assert!(pages[0].contains("symbol=BTC_USDT") && pages[0].contains("page_size=20"));
    assert_eq!(query_number(pages[1], "page_num"), 2);
    server.abort();
}

/// Roughly half this venue settles four-hourly, and the interval on the wire
/// is the one the venue stamped on that settlement, not the eight hours the
/// market-data feed assumes.
#[tokio::test(start_paused = true)]
async fn a_four_hourly_contract_keeps_its_own_settlement_interval() {
    let _io = crate::test_io::IoProgress::new();
    let (client, _requests, server) = http_source(recorded_venue).await;
    let venue = MexcPublicVenue::for_http_test(client);
    let (rows, _) = venue
        .fetch_funding(
            "FORMUSDT",
            1_788_667_200_000,
            1_788_940_800_000,
            20,
            Some(8),
        )
        .await
        .unwrap();
    assert_eq!(rows.len(), 20);
    assert!(rows
        .iter()
        .all(|row| row.funding_interval_hour == Some(Value::from(4))));
    let normalized =
        crate::normalize::normalize_funding_rows("FORMUSDT", 1_788_946_500_000, &rows).unwrap();
    assert!(normalized.iter().all(|row| row.funding_interval_min == 240));
    server.abort();
}

#[tokio::test(start_paused = true)]
async fn the_instrument_lane_reads_the_table_and_the_ticker_lane_reuses_it() {
    let _io = crate::test_io::IoProgress::new();
    let (client, mut requests, server) = http_source(recorded_venue).await;
    let venue = MexcPublicVenue::for_http_test(client);
    let fetched = venue.instruments(1).await.unwrap();
    assert_eq!(fetched.rows.len(), 27);
    assert!(fetched.available_at_ms >= fetched.observed_ts_ms);
    let followed = ["BTCUSDT".to_owned(), "PEPEUSDT".to_owned()]
        .into_iter()
        .collect::<BTreeSet<_>>();
    let tickers = venue.ticker_snapshot(followed).await.unwrap();
    assert_eq!(
        tickers
            .rows
            .iter()
            .map(|row| row.symbol.as_str())
            .collect::<Vec<_>>(),
        vec!["BTCUSDT", "PEPEUSDT"]
    );
    let targets = std::iter::from_fn(|| requests.try_recv().ok()).collect::<Vec<_>>();
    assert_eq!(
        targets
            .iter()
            .filter(|target| target.starts_with(PATH_DETAIL))
            .count(),
        1,
        "the ticker lane must not read the contract list again: {targets:?}"
    );
    assert_eq!(venue.cached_table().len(), 27);
    server.abort();
}

#[tokio::test(start_paused = true)]
async fn a_contract_list_with_nothing_readable_fails_the_instrument_read() {
    let _io = crate::test_io::IoProgress::new();
    let (client, _requests, server) = http_source(|target: &str| {
        if target.starts_with(PATH_DETAIL) {
            // Real rows with the multiplier removed: a contract size is never
            // defaulted, so none of them is readable.
            let mut payload = json(DETAIL);
            for row in payload["data"].as_array_mut().unwrap() {
                row["contractSize"] = Value::from(0);
            }
            return payload;
        }
        recorded_venue(target)
    })
    .await;
    let venue = MexcPublicVenue::for_http_test(client);
    let Err(error) = venue.instruments(1).await else {
        panic!("a page with no readable contract must not become a snapshot");
    };
    assert!(
        error.to_string().contains("no readable contract"),
        "{error}"
    );
    server.abort();
}
