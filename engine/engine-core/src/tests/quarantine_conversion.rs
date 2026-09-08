use super::*;
use crate::{attribution::Attribution, execution::Fills};
use engine_types::numeric::{Exact, ExactNumber};
use engine_types::strategy_process::CallbackOrderOrigin;
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Seek, Write};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

struct Scratch(PathBuf);
impl Scratch {
    fn new(parent: &Path, label: &str) -> Self {
        let path = parent.join(format!("conversion-{label}-{}", std::process::id()));
        fs::create_dir(&path).unwrap();
        Self(path)
    }
}
impl Drop for Scratch {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}

fn first_frame(path: &Path) -> Vec<u8> {
    let mut file = File::open(path).unwrap();
    let mut header = [0u8; 16];
    file.read_exact(&mut header).unwrap();
    let length = u32::from_le_bytes(header[8..12].try_into().unwrap()) as u64;
    assert!(length > 0 && length <= file.metadata().unwrap().len() - 16);
    let mut frame = header.to_vec();
    file.take(length).read_to_end(&mut frame).unwrap();
    assert_eq!(frame.len() as u64, length + 16);
    frame
}

fn frame_kind(frame: &[u8]) -> String {
    #[derive(serde::Deserialize)]
    struct Kind {
        kind: String,
    }
    serde_json::from_slice::<Kind>(&frame[16..]).unwrap().kind
}

fn copy_prefix(chain: &[(u64, PathBuf)], boundary: u64, head: &[u8], directory: &Path) -> PathBuf {
    fs::create_dir(directory).unwrap();
    for (index, source) in chain.iter().take_while(|(index, _)| *index <= boundary) {
        let destination = directory.join(source.file_name().unwrap());
        if *index < boundary {
            // WalWriter opens only the independently copied, trusted newest head.
            fs::hard_link(source, destination).unwrap();
        } else {
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&destination)
                .unwrap()
                .write_all(head)
                .unwrap();
            assert_ne!(
                fs::metadata(source).unwrap().ino(),
                fs::metadata(&destination).unwrap().ino()
            );
            let (rows, torn) = engine_wal::replay_scan(&destination).unwrap();
            assert!(!torn);
            assert_eq!(rows.len(), 1);
            assert!(matches!(rows[0].1, WalRecord::SegmentBase { .. }));
        }
    }
    directory.join(chain[0].1.file_name().unwrap())
}

#[derive(Debug, Default, PartialEq, Eq)]
struct Retrievals {
    queued: usize,
    prepared: usize,
    sources: usize,
}

fn reject_original_v5(frame: &[u8], path: &Path) {
    OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .unwrap()
        .write_all(frame)
        .unwrap();
    let error = match crate::assembly::wal(path) {
        Ok(_) => panic!("ordinary boot accepted a retained v5 base"),
        Err(error) => error,
    };
    assert!(
        matches!(&error, engine_types::WalError::Corrupt { offset: 8, detail }
            if detail.contains("segment_base_v5")),
        "v5 refusal must name the unsupported record, not a checksum or I/O failure: {error}"
    );
    assert_eq!(fs::read(path).unwrap(), frame);
}

fn check_callbacks(wal: &mut engine_wal::WalWriter, base: &WalRecord, counts: &mut Retrievals) {
    let WalRecord::SegmentBase {
        strategy_callback_queues: slots,
        strategy_callback_sources: sources,
        ..
    } = base
    else {
        panic!()
    };
    let mut reader = wal.callback_reader().unwrap().unwrap();
    for slot in slots {
        let queued = reader.read_callback(slot.queued, slot.callback_id).unwrap();
        assert_eq!(
            (queued.callback_id, queued.strategy),
            (slot.callback_id, slot.strategy)
        );
        assert_eq!(
            crate::callback_recovery::paging::CallbackPages::hash(&queued).unwrap(),
            slot.event_sha256
        );
        assert_eq!(
            queued.snapshot().is_some(),
            slot.prepared == Some(slot.queued)
        );
        counts.queued += 1;
        if let Some(cursor) = slot.prepared {
            let prepared = reader.read_callback(cursor, slot.callback_id).unwrap();
            assert_eq!(
                (prepared.callback_id, prepared.strategy),
                (slot.callback_id, slot.strategy)
            );
            assert_eq!(prepared.event, queued.event);
            assert_eq!(prepared.order_origin, queued.order_origin);
            assert!(prepared.snapshot().is_some());
            assert_eq!(
                crate::callback_recovery::paging::CallbackPages::hash(&prepared).unwrap(),
                slot.event_sha256
            );
            counts.prepared += 1;
        }
    }
    for source in sources {
        let mut cursor = source.cursor;
        loop {
            let position = CallbackOrderOrigin {
                segment: cursor.segment,
                sequence: cursor.sequence,
            };
            if position > source.latest {
                break;
            }
            let row = reader
                .next(cursor)
                .unwrap()
                .expect("converted retained source");
            assert!((row.next.segment, row.next.sequence) > (cursor.segment, cursor.sequence));
            counts.sources += usize::from(row.source.is_some());
            cursor = row.next;
        }
    }
}

fn mock_from_base(record: &WalRecord, realm: engine_venue::VenueRealm) -> MockVenue {
    let WalRecord::SegmentBase {
        symbols,
        wall_ts_ms,
        instrument_catalog: Some(catalog),
        open_orders,
        ..
    } = record
    else {
        panic!("base needs retained native catalog")
    };
    let (mut venue, _) = MockVenue::new(
        tape(),
        &symbols.iter().map(String::as_str).collect::<Vec<_>>(),
    );
    venue.identity = Some(AccountIdentity {
        venue: "bybit".into(),
        realm: realm.as_str().into(),
        user_id: "wal-derived-mock".into(),
    });
    venue.catalog_adapter = Some(Box::new(engine_venue::BybitGateway::for_test(
        realm.rest_base(),
        realm,
        engine_venue::Credentials::new(&realm.to_string(), false, "fixture-key", "fixture-secret"),
        symbols.clone(),
    )));
    venue.rules = catalog.rules.clone();
    venue.exact_specs = Some(catalog.specs.clone());
    let specs: BTreeMap<_, _> = symbols
        .iter()
        .enumerate()
        .filter_map(|(index, name)| {
            catalog
                .specs
                .iter()
                .find(|(symbol, _)| symbol == name)
                .map(|(_, spec)| (SymbolId(index as u16), spec.clone()))
        })
        .collect();
    let records = std::slice::from_ref(record);
    let adoption = crate::legacy_quantity::plan(records, &specs, *wall_ts_ms).unwrap();
    let (physical, stops, _, _) =
        crate::reconcile::position_state_with_adoption(records, adoption.as_ref(), false).unwrap();
    let positions = physical
        .into_iter()
        .filter(|(_, quantity)| !quantity.is_zero())
        .map(|(symbol, quantity)| {
            let side = if quantity.is_positive() {
                Side::Buy
            } else {
                Side::Sell
            };
            let stop = stops
                .get(&symbol)
                .filter(|stop| stop.side == side)
                .map_or(0.0, |stop| stop.trigger_px);
            engine_types::PositionView {
                symbol,
                side,
                qty: quantity.abs().to_f64().unwrap(),
                entry_px: 0.0,
                exact_amounts: Some(Box::new(engine_types::risk::PositionAmounts {
                    liquidation_price: None,
                    mark_price: None,
                    quantity: ExactNumber::derived(quantity.abs()),
                    entry_price: ExactNumber::derived(Exact::zero()),
                })),
                stop_px: stop,
                exact_stop_px: Some(Box::new(Exact::from_legacy_f64(stop).unwrap())),
                stop_attached: stop > 0.0,
                leverage: None,
            }
        })
        .collect();
    venue.account_readings.lock().unwrap().push_back(positions);
    venue.working = open_orders
        .iter()
        .filter(|order| order.terminal.is_none() && order.acked)
        .map(|order| VenueOrder {
            client_order_id: order.request.client_order_id.clone(),
            symbol: symbols[order.request.symbol.idx()].clone(),
            side: order.request.side,
            qty: order.request.qty,
            filled_qty: order.filled_qty,
            reduce_only: order.request.reduce_only,
        })
        .collect();
    venue
}

async fn boot(
    path: &Path,
    wal: engine_wal::WalWriter,
    record: WalRecord,
    config: &crate::config::LoadedConfig,
    realm: engine_venue::VenueRealm,
) -> WalRecord {
    let WalRecord::SegmentBase {
        strategies: sleeves,
        ..
    } = &record
    else {
        panic!()
    };
    let records = std::slice::from_ref(&record);
    let configured_keys = config
        .config
        .strategies
        .iter()
        .map(|strategy| strategy.sleeve_name().to_string())
        .collect::<Vec<_>>();
    let plan = crate::identities::plan_identities(
        records,
        &configured_keys,
        None,
        &Default::default(),
        &[],
    )
    .unwrap();
    let strategies =
        crate::assembly::strategies_for_registry(&config.config.strategies, &plan, records)
            .unwrap();
    let venue = mock_from_base(&record, realm);
    let calls = venue.tape.clone();
    let (risk, _) = MockRisk::with(allow_all());
    let mut settings = config.config.engine.clone();
    settings.wal_path = path.into();
    settings.signal_spool_path = Some(path.parent().unwrap().join("signals"));
    settings.control_spool_path = Some(path.parent().unwrap().join("controls"));
    settings.heartbeat_path = None;
    settings.trades_path = None;
    let mut engine = Engine::boot_as_exact(
        &settings,
        &config.sha256,
        wal,
        risk,
        venue,
        strategies,
        sleeves,
        records,
    )
    .await
    .unwrap_or_else(|error| panic!("{}: {error}", path.display()));
    let snapshot = engine.rotation_base(clock::wall_ms());
    engine.wal.barrier().unwrap();
    let calls = calls.lock().unwrap().clone();
    assert_eq!(
        calls
            .iter()
            .filter(|call| matches!(call, Step::ReadAccount))
            .count(),
        1,
        "boot must consume the supplied WAL-derived account response exactly once"
    );
    snapshot
}

fn archived_legacy_request(chain: &[(u64, PathBuf)]) -> OrderRequest {
    let mut candidate: Option<OrderRequest> = None;
    for (_, path) in chain {
        let mut file = File::open(path).unwrap();
        let mut magic = [0; 8];
        file.read_exact(&mut magic).unwrap();
        assert_eq!(&magic, b"EWAL0001");
        let length = file.metadata().unwrap().len();
        while file.stream_position().unwrap() < length {
            let mut header = [0; 8];
            file.read_exact(&mut header).unwrap();
            let size = u32::from_le_bytes(header[..4].try_into().unwrap()) as usize;
            assert!(size > 0 && size as u64 <= length - file.stream_position().unwrap());
            let mut frame = magic.to_vec();
            frame.extend_from_slice(&header);
            frame.resize(size + 16, 0);
            file.read_exact(&mut frame[16..]).unwrap();
            let kind = frame_kind(&frame);
            if matches!(kind.as_str(), "order_sent" | "order_sent_v2") {
                #[derive(serde::Deserialize)]
                struct Sent {
                    request: OrderRequest,
                }
                // This chooses one query; the production lookup validates its source frame.
                let Sent { request } = serde_json::from_slice(&frame[16..]).unwrap();
                candidate = (request.exact_terms.is_none()
                    && matches!(request.kind, OrderKind::Market))
                .then_some(request);
            } else if matches!(
                kind.as_str(),
                "order_update" | "order_update_v2" | "order_update_v3"
            ) {
                #[derive(serde::Deserialize)]
                struct Update {
                    update: OrderUpdate,
                }
                let Update { update } = serde_json::from_slice(&frame[16..]).unwrap();
                let terminal_or_filled = match &update {
                    OrderUpdate::Fill {
                        client_order_id,
                        qty,
                        ..
                    } if *qty > 0.0 => Some(client_order_id),
                    OrderUpdate::Cancelled {
                        client_order_id, ..
                    }
                    | OrderUpdate::Reject {
                        client_order_id, ..
                    } => Some(client_order_id),
                    _ => None,
                };
                if candidate
                    .as_ref()
                    .is_some_and(|request| terminal_or_filled == Some(&request.client_order_id))
                {
                    return candidate.unwrap();
                }
            }
        }
    }
    panic!("captured family contains no legacy market request with observed terminal/fill state")
}

#[tokio::test(start_paused = true)]
#[ignore = "requires original/converted complete quarantine families and strategy configuration"]
async fn converted_v7_bases_boot_and_archived_fills_remain_readable() {
    let env_path = |name| PathBuf::from(std::env::var_os(name).expect(name));
    let original = env_path("TIER1_WAL_CONVERSION_ORIGINAL");
    let converted = env_path("TIER1_WAL_CONVERSION_CONVERTED");
    let config = crate::config::load(&env_path("TIER1_WAL_CONVERSION_CONFIG")).unwrap();
    let expected: usize = std::env::var("TIER1_WAL_CONVERSION_EXPECTED_BASES")
        .unwrap()
        .parse()
        .unwrap();
    let (realm, expected_id, expected_rows, expected_filled, expected_callbacks) =
        match config.config.engine.venue.as_str() {
            "bybit_demo" => (
                engine_venue::VenueRealm::Demo,
                "eng-1786752177403-3",
                3,
                110.0,
                4,
            ),
            "bybit_mainnet" | "bybit" => (
                engine_venue::VenueRealm::Mainnet,
                "eng-1787357335566-5",
                13,
                1.2,
                5,
            ),
            other => panic!("unexpected captured venue {other}"),
        };
    let old_chain = engine_wal::segments(&original).unwrap();
    let new_chain = engine_wal::segments(&converted).unwrap();
    assert_eq!(
        old_chain.iter().map(|(id, _)| id).collect::<Vec<_>>(),
        new_chain.iter().map(|(id, _)| id).collect::<Vec<_>>()
    );
    let source_metadata: Vec<_> = old_chain
        .iter()
        .chain(&new_chain)
        .map(|(_, path)| {
            let metadata = fs::metadata(path).unwrap();
            (path.clone(), metadata.len(), metadata.modified().unwrap())
        })
        .collect();
    let scratch = Scratch::new(original.parent().unwrap(), "converted");
    let mut counts = Retrievals::default();
    let mut bases = 0;
    for ((index, old_path), (_, new_path)) in old_chain.iter().zip(&new_chain) {
        let old_frame = first_frame(old_path);
        if frame_kind(&old_frame) != "segment_base_v5" {
            continue;
        }
        let new_frame = first_frame(new_path);
        assert_eq!(frame_kind(&new_frame), "segment_base_v7");
        let boundary = Scratch::new(&scratch.0, &format!("base-{index}"));
        reject_original_v5(&old_frame, &boundary.0.join("original.wal"));
        let path = copy_prefix(
            &new_chain,
            *index,
            &new_frame,
            &boundary.0.join("converted"),
        );
        let (mut wal, mut records) = crate::assembly::wal(&path).unwrap();
        let base = records.pop().unwrap();
        assert!(records.is_empty());
        check_callbacks(&mut wal, &base, &mut counts);
        let WalRecord::SegmentBase { wall_ts_ms, .. } = &base else {
            panic!()
        };
        let wall_ns = u64::try_from(*wall_ts_ms).unwrap() * 1_000_000;
        let snapshot = {
            let _clock = engine_types::clock::install_virtual(wall_ns, 1_000_000_000).unwrap();
            boot(&path, wal, base, &config, realm).await
        };
        let (replayed, torn) = engine_wal::replay_current(&path).unwrap();
        assert!(!torn && !replayed.is_empty());
        let replayed = replayed
            .into_iter()
            .map(|(_, record)| record)
            .collect::<Vec<_>>();
        let snapshot = std::slice::from_ref(&snapshot);
        assert_eq!(
            Attribution::try_from_records(&replayed).unwrap().snapshot(),
            Attribution::try_from_records(snapshot).unwrap().snapshot(),
            "base {index}: durable attribution must match the boot rotation state"
        );
        assert_eq!(
            Fills::try_from_records(&replayed)
                .unwrap()
                .open_trade_lots(),
            Fills::try_from_records(snapshot).unwrap().open_trade_lots(),
            "base {index}: durable quantities and cost basis must match the boot rotation state"
        );
        assert_eq!(
            crate::reconcile::position_state_with_adoption(&replayed, None, false)
                .unwrap()
                .0,
            crate::reconcile::position_state_with_adoption(snapshot, None, false)
                .unwrap()
                .0,
            "base {index}: durable physical exposure must match the boot rotation state"
        );
        bases += 1;
        eprintln!("{realm} base {index}: original v5 refused unchanged; converted v7 boot and durable state agree");
    }
    assert_eq!(bases, expected);
    assert_eq!(
        counts,
        Retrievals {
            queued: expected_callbacks,
            prepared: expected_callbacks,
            sources: 0,
        }
    );
    // One request per family bounds full archive walks independently of its order count.
    let request = archived_legacy_request(&new_chain);
    let id = &request.client_order_id;
    assert_eq!(id, expected_id);
    let directory = scratch.0.join("lookup-converted");
    let (index, source) = new_chain.last().unwrap();
    let path = copy_prefix(&new_chain, *index, &first_frame(source), &directory);
    let (mut wal, _) = engine_wal::open_current(path).unwrap();
    // The private head grows only after boot's bounded one-record scan. No appends follow.
    let head = directory.join(source.file_name().unwrap());
    assert_ne!(
        fs::metadata(source).unwrap().ino(),
        fs::metadata(&head).unwrap().ino()
    );
    fs::copy(source, head).unwrap();
    let mut reader = wal.order_lineage_reader(id).unwrap().unwrap();
    drop(wal);
    let mut lookup_rows = 0;
    let mut order_state = crate::inflight::LedgerOfOrders::default();
    let mut sent_seen = false;
    while let Some(record) = reader.next().unwrap() {
        if let WalRecord::OrderSent { request: found, .. } = &record {
            assert_eq!(found, &request);
            sent_seen = true;
        }
        order_state.try_apply(&record).unwrap();
        lookup_rows += 1;
    }
    assert_eq!(lookup_rows, expected_rows);
    assert!(sent_seen);
    assert_eq!(order_state.orders[id].request, request);
    assert_eq!(
        order_state.orders[id].ending,
        Some(crate::inflight::Ending::Filled)
    );
    assert_eq!(
        order_state.orders[id].filled_qty().unwrap(),
        expected_filled
    );
    eprintln!(
        "archived {id}: rows={lookup_rows}, ending={:?}, filled={:?}",
        order_state.orders[id].ending,
        order_state.orders[id].filled_qty()
    );
    for (path, length, modified) in source_metadata {
        let metadata = fs::metadata(path).unwrap();
        assert_eq!(
            (metadata.len(), metadata.modified().unwrap()),
            (length, modified)
        );
    }
    eprintln!("{realm}: refused_v5={bases}, converted_boots={bases}, queued={}, prepared={}, source_events={}, archived_ids=1, archived_rows={lookup_rows}", counts.queued, counts.prepared, counts.sources);
    eprintln!("scope: candidate-image converted WAL boot and readers, with WAL-derived account, orders, catalog and mocked collateral; one supplied account response per boot; unknown entry price remains zero; no cross-image snapshot or venue-mutation equality, future executions, network, or account-truth claim; real source-frontier coverage is zero");
}
