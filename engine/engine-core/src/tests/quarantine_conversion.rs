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

#[derive(Default)]
struct Retrievals {
    queued: usize,
    prepared: usize,
    sources: usize,
}

fn compare_callbacks(
    original: &mut engine_wal::WalWriter,
    converted: &mut engine_wal::WalWriter,
    a: &WalRecord,
    b: &WalRecord,
    counts: &mut Retrievals,
) {
    let WalRecord::SegmentBase {
        strategy_callback_queues: old_slots,
        strategy_callback_sources: old_sources,
        ..
    } = a
    else {
        panic!()
    };
    let WalRecord::SegmentBase {
        strategy_callback_queues: new_slots,
        strategy_callback_sources: new_sources,
        ..
    } = b
    else {
        panic!()
    };
    assert_eq!(old_slots.len(), new_slots.len());
    assert_eq!(old_sources.len(), new_sources.len());
    let mut old = original.callback_reader().unwrap().unwrap();
    let mut new = converted.callback_reader().unwrap().unwrap();
    for (a, b) in old_slots.iter().zip(new_slots) {
        assert_eq!(
            old.read_callback(a.queued, a.callback_id).unwrap(),
            new.read_callback(b.queued, b.callback_id).unwrap()
        );
        counts.queued += 1;
        assert_eq!(a.prepared.is_some(), b.prepared.is_some());
        if let (Some(a_cursor), Some(b_cursor)) = (a.prepared, b.prepared) {
            assert_eq!(
                old.read_callback(a_cursor, a.callback_id).unwrap(),
                new.read_callback(b_cursor, b.callback_id).unwrap()
            );
            counts.prepared += 1;
        }
    }
    for (a, b) in old_sources.iter().zip(new_sources) {
        assert_eq!(
            (a.strategy, a.accepted, a.latest),
            (b.strategy, b.accepted, b.latest)
        );
        let (mut a_cursor, mut b_cursor) = (a.cursor, b.cursor);
        loop {
            if (CallbackOrderOrigin {
                segment: a_cursor.segment,
                sequence: a_cursor.sequence,
            }) > a.latest
            {
                break;
            }
            let a_row = old
                .next(a_cursor)
                .unwrap()
                .expect("original retained source");
            let b_row = new
                .next(b_cursor)
                .unwrap()
                .expect("converted retained source");
            assert_eq!(
                (a_row.cursor.segment, a_row.cursor.sequence),
                (b_row.cursor.segment, b_row.cursor.sequence)
            );
            assert_eq!(a_row.source, b_row.source);
            counts.sources += usize::from(a_row.source.is_some());
            a_cursor = a_row.next;
            b_cursor = b_row.next;
        }
    }
}

fn normalized(mut snapshot: WalRecord) -> WalRecord {
    let WalRecord::SegmentBase {
        strategy_callback_queues,
        strategy_callback_sources,
        ..
    } = &mut snapshot
    else {
        panic!()
    };
    for cursor in strategy_callback_queues
        .iter_mut()
        .flat_map(|slot| std::iter::once(&mut slot.queued).chain(slot.prepared.as_mut()))
        .chain(
            strategy_callback_sources
                .iter_mut()
                .map(|source| &mut source.cursor),
        )
    {
        cursor.offset = 0;
    }
    snapshot
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
    source: &WalRecord,
    config: &crate::config::LoadedConfig,
    realm: engine_venue::VenueRealm,
) -> (WalRecord, serde_json::Value, Vec<Step>) {
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
    let venue = mock_from_base(source, realm);
    let calls = venue.tape.clone();
    let sends = venue.sends.clone();
    let cancels = venue.cancels.clone();
    let amends = venue.amends.clone();
    let stops = venue.exact_stops.clone();
    let leverages = venue.leverages.clone();
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
    let snapshot = normalized(engine.rotation_base(clock::wall_ms()));
    engine.wal.barrier().unwrap();
    let mutations = serde_json::json!({"sends":*sends.lock().unwrap(), "cancels":*cancels.lock().unwrap(),
        "amends":*amends.lock().unwrap(), "stops":*stops.lock().unwrap(), "leverages":*leverages.lock().unwrap()});
    let calls = calls.lock().unwrap().clone();
    assert_eq!(
        calls
            .iter()
            .filter(|call| matches!(call, Step::ReadAccount))
            .count(),
        1,
        "boot must consume the supplied WAL-derived account response exactly once"
    );
    (snapshot, mutations, calls)
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
async fn copied_v5_bases_boot_and_archive_lookup_preserve_state() {
    let env_path = |name| PathBuf::from(std::env::var_os(name).expect(name));
    let original = env_path("TIER1_WAL_CONVERSION_ORIGINAL");
    let converted = env_path("TIER1_WAL_CONVERSION_CONVERTED");
    let config = crate::config::load(&env_path("TIER1_WAL_CONVERSION_CONFIG")).unwrap();
    let expected: usize = std::env::var("TIER1_WAL_CONVERSION_EXPECTED_BASES")
        .unwrap()
        .parse()
        .unwrap();
    let realm = match config.config.engine.venue.as_str() {
        "bybit_demo" => engine_venue::VenueRealm::Demo,
        "bybit_mainnet" | "bybit" => engine_venue::VenueRealm::Mainnet,
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
    let scratch = Scratch::new(original.parent().unwrap(), "pairs");
    let mut counts = Retrievals::default();
    let mut bases = 0;
    let mut archived_request = None;
    for ((index, old_path), (_, new_path)) in old_chain.iter().zip(&new_chain) {
        let old_frame = first_frame(old_path);
        if frame_kind(&old_frame) != "segment_base_v5" {
            continue;
        }
        let new_frame = first_frame(new_path);
        assert_eq!(frame_kind(&new_frame), "segment_base_v7");
        let pair = Scratch::new(&scratch.0, &format!("base-{index}"));
        let old_path = copy_prefix(&old_chain, *index, &old_frame, &pair.0.join("original"));
        let new_path = copy_prefix(&new_chain, *index, &new_frame, &pair.0.join("converted"));
        let (mut old_wal, mut old_records) = crate::assembly::wal(&old_path).unwrap();
        let (mut new_wal, mut new_records) = crate::assembly::wal(&new_path).unwrap();
        let old = old_records.pop().unwrap();
        let new = new_records.pop().unwrap();
        assert!(old_records.is_empty() && new_records.is_empty());
        if bases == 0 {
            let WalRecord::SegmentBase { open_orders, .. } = &old else {
                panic!()
            };
            archived_request = open_orders
                .iter()
                .find(|order| {
                    order.request.exact_terms.is_none()
                        && matches!(order.request.kind, OrderKind::Market)
                        && (order.terminal.is_some() || order.filled_qty > 0.0)
                })
                .map(|order| order.request.clone());
        }
        compare_callbacks(&mut old_wal, &mut new_wal, &old, &new, &mut counts);
        assert_eq!(
            Attribution::try_from_records(std::slice::from_ref(&old))
                .unwrap()
                .snapshot(),
            Attribution::try_from_records(std::slice::from_ref(&new))
                .unwrap()
                .snapshot()
        );
        assert_eq!(
            Fills::try_from_records(std::slice::from_ref(&old))
                .unwrap()
                .open_trade_lots(),
            Fills::try_from_records(std::slice::from_ref(&new))
                .unwrap()
                .open_trade_lots()
        );
        let WalRecord::SegmentBase { wall_ts_ms, .. } = &old else {
            panic!()
        };
        let wall_ns = u64::try_from(*wall_ts_ms).unwrap() * 1_000_000;
        let before = {
            let _clock = engine_types::clock::install_virtual(wall_ns, 1_000_000_000).unwrap();
            boot(&old_path, old_wal, old.clone(), &old, &config, realm).await
        };
        let after = {
            let _clock = engine_types::clock::install_virtual(wall_ns, 1_000_000_000).unwrap();
            boot(&new_path, new_wal, new, &old, &config, realm).await
        };
        assert_eq!(
            before, after,
            "base {index}: full boot state and venue mutations"
        );
        bases += 1;
        eprintln!("{realm} base {index}: exact paired boot state preserved");
    }
    assert_eq!(bases, expected);
    let request = archived_request.unwrap_or_else(|| archived_legacy_request(&old_chain));
    let id = &request.client_order_id;
    // One request per family bounds full archive walks independently of its order count.
    let mut readers = Vec::new();
    for (label, chain) in [
        ("lookup-original", &old_chain),
        ("lookup-converted", &new_chain),
    ] {
        let directory = scratch.0.join(label);
        let (index, source) = chain.last().unwrap();
        let path = copy_prefix(chain, *index, &first_frame(source), &directory);
        let (mut wal, _) = engine_wal::open_current(path).unwrap();
        // The private head grows only after boot's bounded one-record scan. No appends follow.
        let head = directory.join(source.file_name().unwrap());
        assert_ne!(
            fs::metadata(source).unwrap().ino(),
            fs::metadata(&head).unwrap().ino()
        );
        fs::copy(source, head).unwrap();
        readers.push(wal.order_lineage_reader(id).unwrap().unwrap());
    }
    let mut lookup_rows = 0;
    let mut order_state = crate::inflight::LedgerOfOrders::default();
    let mut sent_seen = false;
    loop {
        let before = readers[0].next().unwrap();
        let after = readers[1].next().unwrap();
        assert_eq!(before, after, "archived request {id}");
        if before.is_none() {
            break;
        }
        if let Some(WalRecord::OrderSent { request: found, .. }) = &before {
            assert_eq!(found, &request);
            sent_seen = true;
        }
        order_state.try_apply(&before.unwrap()).unwrap();
        lookup_rows += 1;
    }
    assert!(lookup_rows > 1 && sent_seen);
    assert_eq!(order_state.orders[id].request, request);
    assert!(
        order_state.orders[id].ending.is_some()
            || order_state.orders[id].filled_qty().unwrap() > 0.0,
        "archived lookup must preserve observed terminal/fill state"
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
    eprintln!("{realm}: bases={bases}, queued={}, prepared={}, source_events={}, archived_ids=1, archived_rows={lookup_rows}", counts.queued, counts.prepared, counts.sources);
    eprintln!("scope: real copied WAL boot and readers; identical WAL-derived account, orders, catalog and mocked collateral; unknown entry price remains zero, no future executions or network; zero callback counts are covered only by synthetic fixtures");
}
