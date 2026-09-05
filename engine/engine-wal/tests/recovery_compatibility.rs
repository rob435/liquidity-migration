#[test]
fn current_reader_accepts_the_exact_recovery_and_emergency_role_compatibility_probes() {
    let request = serde_json::json!({"client_order_id":"eng-net","strategy":0,"symbol":0,"side":"Sell","qty":1.0,"kind":"Market","stop":null,"reduce_only":true,"close_position":false,"sleeve_effect":{"kind":"emergency_net_reduction","emergency_id":7}});
    let intent = serde_json::json!({"strategy":0,"symbol":0,"side":"Sell","qty":1.0,"kind":"Market","stop":null,"reduce_only":true,"tag":"portfolio-emergency","decided_ns":1,"work":null,"leverage":null});
    let recovered = serde_json::json!({"kind":"recovered_fill_v2","callbacks":{"owners":[],"recv_ns":1},"exec_id":"missed","client_order_id":"","symbol":0,"side":"Sell","qty":1.0,"px":100.0,"fee":0.0,"fee_known":false,"is_maker":false,"forced_close":null,"venue_ts_ms":1,"recovered_wall_ts_ms":2});
    let order = serde_json::json!({"kind":"order_sent_v2","request":request,"dispatch":{"intent":intent,"origin_ns":1},"wire_ns":2,"arrival_mid":100.0});
    for row in [recovered, order] {
        let payload = serde_json::to_vec(&row).unwrap();
        let mut bytes = b"EWAL0001".to_vec();
        bytes.extend_from_slice(&(payload.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&crc32c::crc32c(&payload).to_le_bytes());
        bytes.extend_from_slice(&payload);
        let dir = tempfile::TempDir::new().unwrap();
        let path = dir.path().join("protected.wal");
        std::fs::write(&path, &bytes).unwrap();
        let _ = engine_wal::WalWriter::open(&path).unwrap();
        assert_eq!(std::fs::read(&path).unwrap(), bytes);
    }
}
