use std::path::{Path, PathBuf};

pub struct TempPath {
    path: PathBuf,
    _directory: tempfile::TempDir,
}

impl TempPath {
    pub fn path(&self) -> &Path {
        &self.path
    }
}
impl std::ops::Deref for TempPath {
    type Target = Path;
    fn deref(&self) -> &Path {
        &self.path
    }
}
impl AsRef<Path> for TempPath {
    fn as_ref(&self) -> &Path {
        &self.path
    }
}

pub fn temp_path(tag: &str) -> TempPath {
    let directory = tempfile::tempdir().expect("test directory");
    TempPath {
        path: directory.path().join(format!("{tag}.wal")),
        _directory: directory,
    }
}
