//! Print what an append and a durability barrier cost on this box.
//!
//! `cargo run -p engine-wal --release --example wal_cost [appends] [barriers]`

fn main() {
    let mut args = std::env::args().skip(1);
    let appends: usize = args.next().and_then(|a| a.parse().ok()).unwrap_or(100_000);
    let barriers: usize = args.next().and_then(|a| a.parse().ok()).unwrap_or(200);

    let dir = tempfile::tempdir().expect("temp dir");
    let path = dir.path().join("engine.wal");
    let costs = engine_wal::measure(&path, appends, barriers).expect("measure");
    println!("{costs}");
}
