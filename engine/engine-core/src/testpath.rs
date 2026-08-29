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
