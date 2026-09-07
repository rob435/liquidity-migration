use super::*;
use crate::{attribution::Attribution, execution::Fills};
use engine_types::numeric::{AssetId, Exact, ExactInstrumentSpec, ExactNumber, PricePrecision};
use serde_json::{json, Value};
use std::{collections::BTreeMap, path::Path};

struct FixtureBoot<W: Wal = MockWal> {
    engine: Engine<W, MockRisk, MockVenue>,
    records: Vec<WalRecord>,
    sends: Arc<Mutex<Vec<OrderRequest>>>,
    stops: Arc<Mutex<Vec<(SymbolId, engine_types::order_terms::ExactStopTerms)>>>,
}

fn decimal(value: &str) -> Exact {
    Exact::parse_decimal(value).unwrap()
}

fn native_spec(row: &Value) -> ExactInstrumentSpec {
    let quantity = &row["lotSizeFilter"];
    let price = &row["priceFilter"];
    let number =
        |row: &Value, field: &str| row[field].as_str().filter(|s| !s.is_empty()).map(decimal);
    ExactInstrumentSpec {
        native_symbol: row["symbol"].as_str().unwrap().into(),
        base_asset: AssetId::Named(row["baseCoin"].as_str().unwrap().into()),
        quote_asset: AssetId::Named(row["quoteCoin"].as_str().unwrap().into()),
        settlement_asset: AssetId::Named(row["settleCoin"].as_str().unwrap().into()),
        tick_size: number(price, "tickSize"),
        min_price: number(price, "minPrice"),
        max_price: number(price, "maxPrice"),
        price_precision: PricePrecision::Tick,
        qty_step: number(quantity, "qtyStep"),
        min_qty: number(quantity, "minOrderQty"),
        market_qty_step: number(quantity, "qtyStep"),
        market_min_qty: number(quantity, "minOrderQty"),
        max_qty: number(quantity, "maxOrderQty"),
        max_market_qty: number(quantity, "maxMktOrderQty"),
        min_notional: number(quantity, "minNotionalValue"),
        contract_multiplier: Some(Exact::one()),
        fee_assets: None,
        fee_step: None,
    }
}

fn names(records: &[WalRecord]) -> (Vec<String>, Vec<String>) {
    let state = crate::identities::replay_identities(records)
        .unwrap()
        .unwrap();
    (
        state
            .sleeves
            .iter()
            .map(|key| key.as_str().to_owned())
            .collect(),
        state
            .instruments
            .into_iter()
            .map(|row| row.symbol)
            .collect(),
    )
}

fn captured_venue(symbols: &[String], capture: &Value) -> MockVenue {
    let references = symbols.iter().map(String::as_str).collect::<Vec<_>>();
    let (mut venue, _) = MockVenue::new(tape(), &references);
    #[cfg(feature = "bybit")]
    {
        let base = capture["rest_endpoint"].as_str().unwrap();
        let realm = [
            engine_venue::VenueRealm::Demo,
            engine_venue::VenueRealm::Mainnet,
        ]
        .into_iter()
        .find(|realm| realm.rest_base() == base)
        .unwrap();
        venue.identity = Some(AccountIdentity {
            venue: "bybit".into(),
            realm: realm.as_str().into(),
            user_id: "captured-account".into(),
        });
        // Only native catalog decoding/install is delegated; transport stays mocked.
        venue.catalog_adapter = Some(Box::new(engine_venue::BybitGateway::for_test(
            base,
            realm,
            engine_venue::Credentials::new(
                &realm.to_string(),
                false,
                "fixture-key",
                "fixture-secret",
            ),
            symbols.to_vec(),
        )));
    }

    let specifications = capture["instruments"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| (row["symbol"].as_str().unwrap().to_owned(), native_spec(row)))
        .collect::<Vec<_>>();
    venue.rules = specifications
        .iter()
        .map(|(name, spec)| {
            (
                name.clone(),
                InstrumentRule {
                    tick_size: spec.tick_size.as_ref().unwrap().to_f64().unwrap(),
                    qty_step: spec.qty_step.as_ref().unwrap().to_f64().unwrap(),
                    min_qty: spec.min_qty.as_ref().unwrap().to_f64().unwrap(),
                    min_notional: spec
                        .min_notional
                        .as_ref()
                        .map_or(0.0, |n| n.to_f64().unwrap()),
                },
            )
        })
        .collect();
    venue.exact_specs = Some(specifications);
    let side = |row: &Value| match row["side"].as_str().unwrap() {
        "Buy" => Side::Buy,
        "Sell" => Side::Sell,
        other => panic!("unexpected captured side {other}"),
    };
    let positions = capture["positions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| {
            let quantity = row["size"].as_str().unwrap();
            let entry = row["avgPrice"].as_str().unwrap();
            let stop = row["stopLoss"].as_str().unwrap();
            engine_types::PositionView {
                symbol: SymbolId(
                    symbols
                        .iter()
                        .position(|name| name == row["symbol"].as_str().unwrap())
                        .unwrap() as u16,
                ),
                side: side(row),
                qty: quantity.parse().unwrap(),
                entry_px: entry.parse().unwrap(),
                exact_amounts: Some(Box::new(engine_types::risk::PositionAmounts {
                    quantity: ExactNumber::venue_decimal(quantity).unwrap(),
                    entry_price: ExactNumber::venue_decimal(entry).unwrap(),
                })),
                stop_px: stop.parse().unwrap(),
                exact_stop_px: Some(Box::new(decimal(stop))),
                stop_attached: decimal(stop).is_positive(),
                leverage: Some(row["leverage"].as_str().unwrap().parse().unwrap()),
            }
        })
        .collect();
    venue.account_readings.lock().unwrap().push_back(positions);
    venue.working = capture["open_orders"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| VenueOrder {
            client_order_id: row["orderLinkId"].as_str().unwrap().into(),
            symbol: row["symbol"].as_str().unwrap().into(),
            side: side(row),
            qty: row["qty"].as_str().unwrap().parse().unwrap(),
            filled_qty: row["cumExecQty"].as_str().unwrap().parse().unwrap(),
            reduce_only: row["reduceOnly"].as_bool().unwrap(),
        })
        .collect();
    venue
}

async fn boot_fixture(
    directory: &Path,
    config: &crate::config::LoadedConfig,
    capture: &Value,
    prior: Option<Vec<WalRecord>>,
) -> FixtureBoot {
    let path = directory.join("engine.wal");
    let records = prior.unwrap_or_else(|| crate::assembly::wal(&path).unwrap().1);
    let (mut wal, _) = MockWal::new(tape());
    *wal.records.lock().unwrap() = records.clone();
    wal.seq = records.len() as u64;
    boot_wal_fixture(directory, config, capture, wal, records).await
}

async fn boot_disk_fixture(
    directory: &Path,
    config: &crate::config::LoadedConfig,
    capture: &Value,
) -> FixtureBoot<engine_wal::WalWriter> {
    let (wal, records) = crate::assembly::wal(&directory.join("engine.wal")).unwrap();
    boot_wal_fixture(directory, config, capture, wal, records).await
}

async fn boot_wal_fixture<W: Wal>(
    directory: &Path,
    config: &crate::config::LoadedConfig,
    capture: &Value,
    wal: W,
    records: Vec<WalRecord>,
) -> FixtureBoot<W> {
    let path = directory.join("engine.wal");
    let (sleeves, symbols) = names(&records);
    let venue = captured_venue(&symbols, capture);
    let sends = venue.sends.clone();
    let stops = venue.exact_stops.clone();
    let plan =
        crate::identities::plan_identities(&records, &sleeves, None, &Default::default(), &[])
            .unwrap();
    let strategies =
        crate::assembly::strategies_for_registry(&config.config.strategies, &plan, &records)
            .unwrap();
    let mut settings = config.config.engine.clone();
    settings.wal_path = path;
    settings.signal_spool_path = Some(directory.join("signals"));
    settings.control_spool_path = Some(directory.join("controls"));
    settings.heartbeat_path = None;
    settings.trades_path = None;
    let (risk, _) = MockRisk::with(allow_all());
    let engine = Engine::boot_as_exact(
        &settings,
        &config.sha256,
        wal,
        risk,
        venue,
        strategies,
        &sleeves,
        &records,
    )
    .await
    .unwrap_or_else(|error| panic!("{}: {error}", directory.display()));
    assert!(
        sends.lock().unwrap().is_empty(),
        "boot rehearsal must not place orders"
    );
    assert!(
        stops.lock().unwrap().is_empty(),
        "boot has no fresh stop reference"
    );
    FixtureBoot {
        engine,
        records,
        sends,
        stops,
    }
}

fn assert_native_claims(records: &[WalRecord], mappings: &[Value]) {
    let attribution = Attribution::try_from_records(records).unwrap();
    assert_eq!(attribution.snapshot().positions.len(), mappings.len());
    for row in mappings {
        assert_eq!(
            attribution.signed_exact(
                StrategyId(row["strategy_id"].as_u64().unwrap() as u16),
                SymbolId(row["symbol_id"].as_u64().unwrap() as u16),
            ),
            decimal(row["native_signed_quantity"].as_str().unwrap()),
            "{}",
            row["symbol"]
        );
    }
    assert!(attribution.legacy_quantities.is_empty());
}

fn financial_projection(snapshot: &WalRecord) -> Value {
    let WalRecord::SegmentBase {
        portfolio,
        open_trade_lots,
        logged_exposure,
        intended_stops,
        ..
    } = snapshot
    else {
        panic!("rotation base")
    };
    json!({"portfolio":portfolio,"lots":open_trade_lots,"physical":logged_exposure,"stops":intended_stops})
}

async fn verify_rotated_stop_repair(
    directory: &Path,
    config: &crate::config::LoadedConfig,
    capture: &Value,
    snapshot: &WalRecord,
) -> Value {
    let mut missing = capture.clone();
    for position in missing["positions"].as_array_mut().unwrap() {
        position["stopLoss"] = json!("0");
    }
    missing["open_orders"] = json!([]);
    let boot = boot_fixture(directory, config, &missing, Some(vec![snapshot.clone()])).await;
    verify_stop_repairs(capture, snapshot, boot).await
}

struct RepairQuotes(ScriptFeed);

impl MarketFeed for RepairQuotes {
    fn admit(&mut self, symbol: &str, feed: engine_types::Feed) -> Option<SymbolId> {
        self.0.admit(symbol, feed)
    }

    async fn next_event(&mut self) -> Result<MarketEvent, FeedError> {
        tokio::task::yield_now().await;
        let mut event = self
            .0
            .events
            .pop_front()
            .expect("captured held-symbol quotes");
        if let MarketEvent::Quote { quote, .. } = &mut event {
            quote.recv_ns = clock::now_ns();
        }
        self.0.events.push_back(event);
        Ok(event)
    }
}

async fn verify_stop_repairs<W: Wal>(
    capture: &Value,
    snapshot: &WalRecord,
    boot: FixtureBoot<W>,
) -> Value {
    let FixtureBoot {
        mut engine,
        records,
        sends,
        stops,
    } = boot;
    let _io = crate::test_io::IoProgress::new();
    let (_, symbols) = names(&records);
    let mut quotes = RepairQuotes(ScriptFeed {
        events: capture["positions"]
            .as_array()
            .unwrap()
            .iter()
            .map(|position| {
                let name = position["symbol"].as_str().unwrap();
                let price = position["markPrice"].as_str().unwrap().parse().unwrap();
                MarketEvent::Quote {
                    symbol: SymbolId(
                        symbols.iter().position(|symbol| symbol == name).unwrap() as u16
                    ),
                    quote: Quote {
                        bid_px: price,
                        ask_px: price,
                        bid_qty: 1.0,
                        ask_qty: 1.0,
                        recv_ns: clock::now_ns(),
                        ..Default::default()
                    },
                }
            })
            .collect(),
        close_at_end: false,
        symbols,
        admits_wrongly: false,
        admitted: Default::default(),
    });
    let expected_repairs = capture["positions"].as_array().unwrap().len();
    let completed = stops.clone();
    let deadline = std::time::Instant::now() + Duration::from_secs(10);
    engine
        .run(&mut quotes, &mut ScriptOrderFeed::empty(), async move {
            while completed.lock().unwrap().len() < expected_repairs {
                assert!(
                    std::time::Instant::now() < deadline,
                    "native repair I/O did not complete"
                );
                tokio::task::yield_now().await;
            }
        })
        .await
        .unwrap();
    let actual_orders = sends.lock().unwrap().clone();
    assert!(actual_orders.is_empty(), "restoring portfolio protection must not place an opening or reduction order after accepted stop repairs; orders: {actual_orders:?}");
    let repairs = stops.lock().unwrap();
    let before = Attribution::try_from_records(std::slice::from_ref(snapshot))
        .unwrap()
        .snapshot();
    assert_eq!(
        repairs.len(),
        before.positions.len(),
        "all removed native stops must be repaired"
    );
    for position in &before.positions {
        let target = position.stop_px.as_ref().unwrap();
        let (_, repair) = repairs
            .iter()
            .find(|(symbol, _)| *symbol == position.symbol)
            .unwrap();
        let side = if position.signed_qty.is_positive() {
            Side::Buy
        } else {
            Side::Sell
        };
        assert_eq!(repair.position_side, side);
        assert!(
            match side {
                Side::Buy => repair.trigger_price >= *target,
                Side::Sell => repair.trigger_price <= *target,
            },
            "native tick quantization may only tighten protection"
        );
    }
    let repaired = engine.rotation_base(clock::wall_ms());
    assert_eq!(
        Attribution::try_from_records(std::slice::from_ref(&repaired))
            .unwrap()
            .snapshot(),
        before
    );
    assert_eq!(
        Fills::try_from_records(std::slice::from_ref(&repaired))
            .unwrap()
            .open_trade_lots(),
        Fills::try_from_records(std::slice::from_ref(snapshot))
            .unwrap()
            .open_trade_lots()
    );
    json!({"removed_native_stops":before.positions.len(), "exact_repairs":*repairs, "opening_or_reduction_orders":0, "quote_source":"captured mark prices replayed with fresh mocked receipt times"})
}

#[tokio::test(start_paused = true)]
#[ignore = "requires the captured full live WAL and authenticated venue fixture bundle"]
async fn full_live_legacy_quantity_boot_replay_rotation_and_reboot() {
    let root = std::env::var_os("TIER1_LIVE_LEGACY_FIXTURES").expect("TIER1_LIVE_LEGACY_FIXTURES");
    let root = Path::new(&root);
    let mapping: Value =
        serde_json::from_slice(&std::fs::read(root.join("quantity-mappings.json")).unwrap())
            .unwrap();
    let venue: Value =
        serde_json::from_slice(&std::fs::read(root.join("venue.json")).unwrap()).unwrap();
    let fixture_wall_ns = venue["finished_ns"]
        .as_u64()
        .expect("venue capture completion time");
    // Captured strategy holding deadlines and captured market/account facts share this instant.
    let _clock = engine_types::clock::install_virtual(fixture_wall_ns, 1_000_000_000).unwrap();
    let out = root.join(format!(
        "candidate-rehearsal-{}-{}",
        std::process::id(),
        clock::wall_ms()
    ));
    std::fs::create_dir(&out).unwrap();
    let mut reports = Vec::new();
    for (realm, cleared) in [("demo", false), ("mainnet", false), ("demo", true)] {
        let directory = out.join(format!(
            "{realm}-{}",
            if cleared { "cleared" } else { "original" }
        ));
        std::fs::create_dir(&directory).unwrap();
        for entry in std::fs::read_dir(root.join(realm).join("working")).unwrap() {
            let entry = entry.unwrap();
            std::fs::copy(entry.path(), directory.join(entry.file_name())).unwrap();
        }
        let config = crate::config::load(&root.join(realm).join("engine.toml")).unwrap();
        let mappings = mapping["realms"][realm]["owner_native_mappings"]
            .as_array()
            .unwrap();
        let prior = if cleared {
            let (_, mut records) = crate::assembly::wal(&directory.join("engine.wal")).unwrap();
            records.push(WalRecord::LatchCleared {
                wall_ts_ms: clock::wall_ms(),
                note: "rehearsal of separately proven historical ENA stop reconciliation".into(),
                restated_exposure: mappings
                    .iter()
                    .map(|row| {
                        let exact = decimal(row["native_signed_quantity"].as_str().unwrap());
                        engine_types::SymbolTotal {
                            symbol: SymbolId(row["symbol_id"].as_u64().unwrap() as u16),
                            signed_qty: exact.to_f64().unwrap(),
                            exact_signed_qty: Some(ExactNumber::derived(exact)),
                        }
                    })
                    .collect(),
                findings: vec![
                    "ENAUSDT historical unowned physical residue: logged1564, captured native0"
                        .into(),
                ],
            });
            Some(records)
        } else {
            None
        };
        let FixtureBoot {
            mut engine,
            records: before_records,
            ..
        } = boot_fixture(&directory, &config, &venue["realms"][realm], prior).await;
        let before = Attribution::try_from_records(&before_records)
            .unwrap()
            .snapshot();
        let fills_before = Fills::try_from_records(&before_records).unwrap();
        let mut expected_lots = fills_before.open_trade_lots();
        let snapshot = engine.rotation_base(clock::wall_ms());
        let WalRecord::SegmentBase {
            portfolio: Some(after),
            open_trade_lots: Some(lots),
            ..
        } = &snapshot
        else {
            panic!("complete rotation state")
        };
        let mut expected = before.clone();
        for position in &mut expected.positions {
            let row = mappings
                .iter()
                .find(|row| {
                    row["strategy_id"] == position.strategy.0
                        && row["symbol_id"] == position.symbol.0
                })
                .unwrap();
            position.signed_qty = decimal(row["native_signed_quantity"].as_str().unwrap());
        }
        assert_eq!(after, &expected, "{realm}: only eligible quantities may change; accounting, unknown basis, and sleeve stops remain exact");
        for lot in &mut expected_lots {
            let row = mappings
                .iter()
                .find(|row| row["sleeve"] == lot.sleeve && row["symbol"] == lot.symbol)
                .unwrap();
            lot.signed_qty = decimal(row["native_signed_quantity"].as_str().unwrap());
            lot.exact_quantity = true;
        }
        assert_eq!(
            lots, &expected_lots,
            "{realm}: preserve actual cash, entry/exit values, fees, and lot history"
        );
        assert_native_claims(std::slice::from_ref(&snapshot), mappings);
        let physical =
            crate::reconcile::physical_exposure(std::slice::from_ref(&snapshot)).unwrap();
        for row in mappings {
            assert_eq!(
                physical[&SymbolId(row["symbol_id"].as_u64().unwrap() as u16)],
                decimal(row["native_signed_quantity"].as_str().unwrap())
            );
        }
        if realm == "demo" {
            if cleared {
                assert_eq!(physical.len(), 7);
                assert!(
                    !physical.contains_key(&SymbolId(14)),
                    "native clear removes only the unowned ENA residue"
                );
                let WalRecord::SegmentBase { may_open, .. } = &snapshot else {
                    unreachable!()
                };
                assert!(
                    *may_open,
                    "cleared native account must not retain the ghost-history latch"
                );
                assert_eq!(
                    after
                        .positions
                        .iter()
                        .filter(|position| position
                            .stop_px
                            .as_ref()
                            .is_some_and(Exact::is_positive))
                        .count(),
                    7
                );
            } else {
                assert_eq!(
                    physical[&SymbolId(14)],
                    decimal("1564"),
                    "ENA is an independent historical-stop mismatch, not numerical residue"
                );
            }
            let actual = before_records.iter().filter(|record| matches!(record, WalRecord::OrderUpdate { update: OrderUpdate::Fill { exec_id, side: Side::Sell, qty, px, fee: Some(fee), .. }, .. } if exec_id == "f8929fb7-aeda-4dc5-a0eb-67bca018caab" && *qty == 0.01 && *px == 1200.29 && *fee == 0.0066016)).count();
            assert_eq!(
                actual, 1,
                "the actual captured ZEC partial sale and fee remain in the full prefix"
            );
            let zec = lots.iter().find(|lot| lot.symbol == "ZECUSDT").unwrap();
            assert_eq!(zec.signed_qty, decimal("0.28"));
            assert_eq!(zec.fees, Some(Exact::from_legacy_f64(0.0066016).unwrap()));
        }
        let expected_projection = financial_projection(&snapshot);
        engine.wal.barrier().unwrap();
        let booted_records = engine.wal.snapshot_records();
        drop(engine);
        let FixtureBoot {
            engine: second,
            records: adopted,
            ..
        } = boot_fixture(
            &directory,
            &config,
            &venue["realms"][realm],
            Some(booted_records),
        )
        .await;
        let adoption_count = adopted
            .iter()
            .filter(|record| matches!(record, WalRecord::LegacyQuantityGridAdopted { .. }))
            .count();
        assert_eq!(adoption_count, 1, "{realm}: one durable adoption");
        assert_native_claims(&adopted, mappings);
        let second_snapshot = second.rotation_base(clock::wall_ms());
        assert_eq!(
            financial_projection(&second_snapshot),
            expected_projection,
            "{realm}: full-prefix reboot financial state"
        );
        drop(second);
        let rotation_path = directory.join("rotation.wal");
        let (mut writer, _) = engine_wal::WalWriter::open(&rotation_path).unwrap();
        writer.append(&second_snapshot).unwrap();
        writer.barrier().unwrap();
        writer.rotate(&second_snapshot).unwrap();
        drop(writer);
        let (_, rotated) = crate::assembly::wal(&rotation_path).unwrap();
        let FixtureBoot {
            engine: third,
            records: rotated,
            ..
        } = boot_fixture(&directory, &config, &venue["realms"][realm], Some(rotated)).await;
        assert!(
            rotated
                .iter()
                .all(|record| !matches!(record, WalRecord::LegacyQuantityGridAdopted { .. })),
            "{realm}: rotation stores canonical state without another adoption"
        );
        assert_native_claims(&rotated, mappings);
        assert_eq!(
            financial_projection(&third.rotation_base(clock::wall_ms())),
            expected_projection,
            "{realm}: rotated reboot financial state"
        );
        let final_records = third.wal.snapshot_records();
        let third_snapshot = third.rotation_base(clock::wall_ms());
        drop(third);
        assert!(final_records
            .iter()
            .all(|record| !matches!(record, WalRecord::LegacyQuantityGridAdopted { .. })));
        let mut report = json!({"realm":realm,"cleared":cleared,"full_prefix_records":before_records.len(),"positions_checked":mappings.len(),"full_prefix_adoptions":adoption_count,"reboot_and_rotation_projection":expected_projection,"ena_physical_mismatch":if realm=="demo" {json!({"original_wal":"1564","venue":"0","restated":cleared,"excluded_records":0})}else{Value::Null}});
        std::fs::write(
            directory.join("quantity-report.json"),
            serde_json::to_vec_pretty(&report).unwrap(),
        )
        .unwrap();
        if cleared {
            report["stop_repair"] = verify_rotated_stop_repair(
                &directory,
                &config,
                &venue["realms"][realm],
                &third_snapshot,
            )
            .await;
        }
        reports.push(report);
    }
    let report = json!({"scope":"Full copied WAL prefixes, actual configured strategies with embedded callbacks, record-backed mock WAL at boot, real WalWriter serialization/rotation, mocked venue transport and collateral balances; no network or venue mutations. Captured native positions, instrument decimals, and working stops retained.","clock":{"wall_ns":fixture_wall_ns,"source":"venue.json finished_ns","scope":"captured-data instant; no replay beyond native strategy holding deadlines"},"archive_boundary":"Direct actual-WalWriter boot required uncopied retained order-lineage source segment1; archive preservation is not assessed by this current-prefix bundle. No captured callback or other WAL records are filtered.","realms":reports});
    std::fs::write(
        out.join("report.json"),
        serde_json::to_vec_pretty(&report).unwrap(),
    )
    .unwrap();
    eprintln!(
        "live legacy fixture evidence: {}",
        out.join("report.json").display()
    );
}

#[tokio::test(start_paused = true)]
#[ignore = "requires current canonical WAL prefixes and authenticated venue captures"]
async fn current_native_state_boot_rotation_and_reboot() {
    let root =
        std::env::var_os("TIER1_CURRENT_NATIVE_FIXTURES").expect("TIER1_CURRENT_NATIVE_FIXTURES");
    let root = Path::new(&root);
    let capture: Value =
        serde_json::from_slice(&std::fs::read(root.join("venue.json")).unwrap()).unwrap();
    let wall_ns = capture["finished_ns"].as_u64().unwrap();
    let _clock = engine_types::clock::install_virtual(wall_ns, 1_000_000_000).unwrap();
    let out = root.join(format!("canonical-rehearsal-{}", std::process::id()));
    std::fs::create_dir(&out).unwrap();
    for realm in ["demo", "mainnet"] {
        let directory = out.join(realm);
        std::fs::create_dir(&directory).unwrap();
        for entry in std::fs::read_dir(root.join(realm).join("working")).unwrap() {
            let entry = entry.unwrap();
            let destination = directory.join(entry.file_name());
            #[cfg(target_os = "macos")]
            assert!(std::process::Command::new("cp")
                .arg("-c")
                .arg(entry.path())
                .arg(&destination)
                .status()
                .unwrap()
                .success());
            #[cfg(not(target_os = "macos"))]
            std::fs::copy(entry.path(), destination).unwrap();
        }
        let config = crate::config::load(&root.join(realm).join("engine.toml")).unwrap();
        let native = &capture["realms"][realm];
        assert_eq!(native["account_matches_expected"], true);
        let FixtureBoot {
            mut engine,
            records,
            ..
        } = boot_disk_fixture(&directory, &config, native).await;
        let (_, symbols) = names(&records);
        let before = Attribution::try_from_records(&records).unwrap();
        assert!(
            before.legacy_quantities.is_empty(),
            "{realm}: canonical fixture"
        );
        let physical = crate::reconcile::physical_exposure(&records).unwrap();
        let native_positions = native["positions"].as_array().unwrap();
        let expected_physical: BTreeMap<_, _> = native_positions
            .iter()
            .map(|row| {
                let symbol = SymbolId(
                    symbols
                        .iter()
                        .position(|name| name == row["symbol"].as_str().unwrap())
                        .unwrap() as u16,
                );
                let quantity = decimal(row["size"].as_str().unwrap());
                (
                    symbol,
                    if row["side"] == "Sell" {
                        -quantity
                    } else {
                        quantity
                    },
                )
            })
            .collect();
        let nonzero: BTreeMap<_, _> = physical
            .into_iter()
            .filter(|(_, quantity)| !quantity.is_zero())
            .collect();
        assert_eq!(nonzero, expected_physical, "{realm}: captured native net");
        let snapshot = engine.rotation_base(clock::wall_ms());
        assert!(
            matches!(&snapshot, WalRecord::SegmentBase { may_open: true, .. }),
            "{realm}: initial boot reconciles"
        );
        let WalRecord::SegmentBase {
            portfolio: Some(portfolio),
            open_trade_lots: Some(lots),
            ..
        } = &snapshot
        else {
            panic!("complete canonical rotation");
        };
        assert_eq!(
            portfolio,
            &before.snapshot(),
            "{realm}: exact ownership and accounting"
        );
        assert_eq!(
            lots,
            &Fills::try_from_records(&records).unwrap().open_trade_lots(),
            "{realm}: exact open lots"
        );
        let expected = financial_projection(&snapshot);
        engine.wal.barrier().unwrap();
        drop(records);
        drop(engine);
        let FixtureBoot {
            engine: mut second, ..
        } = boot_disk_fixture(&directory, &config, native).await;
        let snapshot = second.rotation_base(clock::wall_ms());
        assert!(
            matches!(&snapshot, WalRecord::SegmentBase { may_open: true, .. }),
            "{realm}: full-prefix reboot reconciles"
        );
        assert_eq!(
            financial_projection(&snapshot),
            expected,
            "{realm}: full-prefix reboot"
        );
        second.wal.rotate(&snapshot).unwrap();
        drop(second);
        let FixtureBoot { engine: third, .. } =
            boot_disk_fixture(&directory, &config, native).await;
        let snapshot = third.rotation_base(clock::wall_ms());
        assert!(
            matches!(&snapshot, WalRecord::SegmentBase { may_open: true, .. }),
            "{realm}: rotated reboot reconciles"
        );
        assert_eq!(
            financial_projection(&snapshot),
            expected,
            "{realm}: rotated reboot"
        );
        drop(third);
        let mut missing = native.clone();
        for position in missing["positions"].as_array_mut().unwrap() {
            position["stopLoss"] = json!("0");
        }
        missing["open_orders"] = json!([]);
        let boot = boot_disk_fixture(&directory, &config, &missing).await;
        let repairs = verify_stop_repairs(native, &snapshot, boot).await;
        eprintln!("{realm}: {} captured native positions; exact ownership, accounting, lots and protection survive full-prefix and rotated reboot; repairs={repairs}", native_positions.len());
    }
    eprintln!("scope: complete retained families through the pinned current prefix, configured embedded strategies, real WAL readers/writer/rotation, mocked transport/risk/collateral; no live mutations or uncaptured future executions");
}
