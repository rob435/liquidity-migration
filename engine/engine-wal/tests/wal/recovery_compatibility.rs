/// Frame one hand-written record and read it back through the WAL reader.
fn read_back(row: &serde_json::Value) -> engine_types::WalRecord {
    let payload = serde_json::to_vec(row).unwrap();
    let mut bytes = b"EWAL0001".to_vec();
    bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
    bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
    bytes.extend_from_slice(&payload);
    let dir = tempfile::TempDir::new().unwrap();
    let path = dir.path().join("protected.wal");
    std::fs::write(&path, &bytes).unwrap();
    let (_, records) = engine_wal::WalWriter::open(&path).unwrap();
    assert_eq!(
        std::fs::read(&path).unwrap(),
        bytes,
        "opening a log must not rewrite the frames it read"
    );
    let [(_, record)] = <[_; 1]>::try_from(records).expect("one record");
    record
}

#[test]
fn current_reader_accepts_the_exact_recovery_and_emergency_role_compatibility_probes() {
    let request = serde_json::json!({"client_order_id":"eng-net","strategy":0,"symbol":0,"side":"Sell","qty":1.0,"kind":"Market","stop":null,"reduce_only":true,"close_position":false,"sleeve_effect":{"kind":"emergency_net_reduction","emergency_id":7}});
    let intent = serde_json::json!({"strategy":0,"symbol":0,"side":"Sell","qty":1.0,"kind":"Market","stop":null,"reduce_only":true,"tag":"portfolio-emergency","decided_ns":1,"work":null,"leverage":null});
    let recovered = serde_json::json!({"kind":"recovered_fill_v2","callbacks":{"owners":[],"recv_ns":1},"exec_id":"missed","client_order_id":"","symbol":0,"side":"Sell","qty":1.0,"px":100.0,"fee":0.0,"fee_known":false,"is_maker":false,"forced_close":null,"venue_ts_ms":1,"recovered_wall_ts_ms":2});
    let order = serde_json::json!({"kind":"order_sent_v2","request":request,"dispatch":{"intent":intent,"origin_ns":1},"wire_ns":2,"arrival_mid":100.0});
    for row in [recovered, order] {
        read_back(&row);
    }
}

/// Every log the funded engine has written so far holds this shape.
#[test]
fn an_intent_frame_written_before_the_cause_field_replays_with_no_cause() {
    let row = serde_json::json!({"kind":"intent","intent":{"strategy":0,"symbol":0,"side":"Buy","qty":1.0,"kind":"Market","stop":null,"reduce_only":false,"tag":"long_native_entry","decided_ns":11,"work":null,"leverage":null}});
    let engine_types::WalRecord::Intent { intent, cause } = read_back(&row) else {
        panic!("intent record");
    };
    assert_eq!(cause, None);
    assert_eq!(intent.decided_ns, 11);
}

#[test]
fn a_typed_refusal_frame_replays_through_the_wal_reader() {
    let row = serde_json::json!({"kind":"intent_refused","wall_ts_ms":1700000000003i64,"strategy":0,"symbol":0,"tag":"long_native_entry","code":"engine_latched","detail":"boot could not account for what this account holds"});
    let engine_types::WalRecord::IntentRefused {
        code,
        client_order_id,
        ..
    } = read_back(&row)
    else {
        panic!("intent_refused record");
    };
    assert_eq!(code, "engine_latched");
    assert_eq!(client_order_id, None);
}
