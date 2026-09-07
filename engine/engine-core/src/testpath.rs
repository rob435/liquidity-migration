//! Throwaway file paths for tests, cleaned up on the way out.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

static COUNTER: AtomicU64 = AtomicU64::new(0);

pub struct TempPath(PathBuf);

impl TempPath {
    pub fn path(&self) -> &Path {
        &self.0
    }
}

impl std::ops::Deref for TempPath {
    type Target = Path;

    fn deref(&self) -> &Path {
        &self.0
    }
}

impl AsRef<Path> for TempPath {
    fn as_ref(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempPath {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

pub fn temp_path(tag: &str) -> TempPath {
    let mut path = std::env::temp_dir();
    let n = COUNTER.fetch_add(1, Ordering::Relaxed);
    path.push(format!(
        "engine-core-{}-{}-{}.wal",
        std::process::id(),
        tag,
        n
    ));
    let _ = std::fs::remove_file(&path);
    TempPath(path)
}

/// Frame historical fixtures without giving the current writer a retired write path.
pub fn append_history(
    wal: &mut engine_wal::WalWriter,
    family: &Path,
    record: &engine_types::WalRecord,
) -> Result<u64, engine_types::WalError> {
    use engine_types::{Wal, WalRecord};
    use std::io::Write;

    if !matches!(record, WalRecord::Retained(_)) {
        return wal.append(record);
    }
    wal.barrier()?;
    let sequence = wal.next_seq();
    let segment = wal.callback_reader()?.unwrap().start().segment;
    let path = engine_wal::segments(family)?
        .into_iter()
        .find(|(index, _)| *index == segment)
        .unwrap()
        .1;
    let payload = serde_json::to_vec(record).unwrap();
    let mut file = std::fs::OpenOptions::new().append(true).open(path)?;
    file.write_all(&(payload.len() as u32).to_le_bytes())?;
    file.write_all(&crc32c::crc32c(&payload).to_le_bytes())?;
    file.write_all(&payload)?;
    file.sync_data()?;
    *wal = engine_wal::open_current(family)?.0;
    Ok(sequence)
}
