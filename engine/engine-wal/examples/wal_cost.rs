//! Print what an append, a durability barrier and a segment rotation cost on
//! this box, on the filesystem holding `dir` (a temporary directory when none
//! is given).
//!
//! `cargo run -p engine-wal --release --example wal_cost [appends] [barriers] [rotations] [dir]`

fn main() {
    let mut args = std::env::args().skip(1);
    let appends: usize = args.next().and_then(|a| a.parse().ok()).unwrap_or(100_000);
    let barriers: usize = args.next().and_then(|a| a.parse().ok()).unwrap_or(200);
    let rotations: usize = args.next().and_then(|a| a.parse().ok()).unwrap_or(20);
    let dir = args.next();

    let temp = match &dir {
        Some(_) => None,
        None => Some(tempfile::tempdir().expect("temp dir")),
    };
    let root = match (&dir, &temp) {
        (Some(dir), _) => std::path::PathBuf::from(dir),
        (_, Some(temp)) => temp.path().to_path_buf(),
        (None, None) => unreachable!("one of the two is always present"),
    };
    let path = root.join("engine.wal");

    println!("path={}", path.display());
    let costs = engine_wal::measure(&path, appends, barriers).expect("measure");
    println!("{costs}");
    for costs in engine_wal::measure_rotation(&path, rotations).expect("measure rotation") {
        println!("{costs}");
    }
}
