use std::fs::{self, File, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{de::DeserializeOwned, Serialize};

use crate::model::NormalizedObservation;
use crate::worker::WorkerError;
use engine_types::MAX_SIGNAL_OBSERVATION_BYTES;

#[derive(Clone, Debug)]
pub struct AtomicJsonStore {
    path: PathBuf,
}

impl AtomicJsonStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn load<T: DeserializeOwned>(&self) -> Result<Option<T>, WorkerError> {
        match self.load_bytes()? {
            Some(bytes) => serde_json::from_slice(&bytes)
                .map(Some)
                .map_err(|error| WorkerError::json("parse durable state", error)),
            None => Ok(None),
        }
    }

    pub fn load_bytes(&self) -> Result<Option<Vec<u8>>, WorkerError> {
        match fs::read(&self.path) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map(|_: serde_json::Value| Some(bytes))
                .map_err(|error| WorkerError::json("parse durable JSON", error)),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(WorkerError::io("read durable state", error)),
        }
    }

    pub fn save<T: Serialize>(&self, value: &T) -> Result<(), WorkerError> {
        let bytes = serde_json::to_vec(value)
            .map_err(|error| WorkerError::json("encode durable state", error))?;
        self.save_bytes(&bytes)
    }

    pub fn save_bytes(&self, bytes: &[u8]) -> Result<(), WorkerError> {
        serde_json::from_slice::<serde_json::Value>(bytes)
            .map_err(|error| WorkerError::json("validate durable JSON", error))?;
        atomic_write(&self.path, bytes)
    }

    pub fn remove(&self) -> Result<(), WorkerError> {
        match fs::remove_file(&self.path) {
            Ok(()) => sync_parent(&self.path),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(WorkerError::io("remove durable file", error)),
        }
    }
}

#[derive(Clone, Debug)]
pub struct SpoolWriter {
    directory: PathBuf,
}

impl SpoolWriter {
    pub fn new(directory: impl Into<PathBuf>) -> Result<Self, WorkerError> {
        let directory = directory.into();
        fs::create_dir_all(&directory)
            .map_err(|error| WorkerError::io("create signal spool", error))?;
        Ok(Self { directory })
    }

    pub fn write(&self, observation: &NormalizedObservation) -> Result<PathBuf, WorkerError> {
        let bytes = serde_json::to_vec(observation)
            .map_err(|error| WorkerError::json("encode signal observation", error))?;
        self.write_encoded(&bytes)
    }

    pub fn write_encoded(&self, bytes: &[u8]) -> Result<PathBuf, WorkerError> {
        let observation: NormalizedObservation = serde_json::from_slice(bytes)
            .map_err(|error| WorkerError::json("parse pending signal observation", error))?;
        if observation.payload.len() > MAX_SIGNAL_OBSERVATION_BYTES {
            return Err(WorkerError::state(format!(
                "pending signal payload exceeds {MAX_SIGNAL_OBSERVATION_BYTES} bytes"
            )));
        }
        let calculated = crate::config::sha256_hex(&observation.canonical_envelope_bytes());
        if calculated != observation.content_sha256 {
            return Err(WorkerError::state(
                "pending signal observation content hash does not match",
            ));
        }
        let name = format!(
            "{:020}-{}.json",
            observation.sequence, observation.content_sha256
        );
        let path = self.directory.join(name);
        if path.exists() {
            let existing = fs::read(&path)
                .map_err(|error| WorkerError::io("read existing signal observation", error))?;
            if existing == bytes {
                return Ok(path);
            }
            return Err(WorkerError::state(format!(
                "signal spool path {} already contains different bytes",
                path.display()
            )));
        }
        atomic_write(&path, bytes)?;
        Ok(path)
    }
}

pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<(), WorkerError> {
    static TEMPORARY_SEQUENCE: AtomicU64 = AtomicU64::new(0);
    let parent = path
        .parent()
        .ok_or_else(|| WorkerError::state("atomic path has no parent"))?;
    fs::create_dir_all(parent)
        .map_err(|error| WorkerError::io("create output directory", error))?;
    let file_name = path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| WorkerError::state("atomic path is not UTF-8"))?;
    let temporary = parent.join(format!(
        ".{file_name}.{}-{}.tmp",
        std::process::id(),
        TEMPORARY_SEQUENCE.fetch_add(1, Ordering::Relaxed)
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|error| WorkerError::io("create atomic temporary file", error))?;
    let result = (|| {
        file.write_all(bytes)
            .map_err(|error| WorkerError::io("write atomic temporary file", error))?;
        file.sync_all()
            .map_err(|error| WorkerError::io("sync atomic temporary file", error))?;
        drop(file);
        fs::rename(&temporary, path)
            .map_err(|error| WorkerError::io("publish atomic file", error))?;
        sync_parent(path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn sync_parent(path: &Path) -> Result<(), WorkerError> {
    let parent = path
        .parent()
        .ok_or_else(|| WorkerError::state("durable path has no parent"))?;
    File::open(parent)
        .and_then(|directory| directory.sync_all())
        .map_err(|error| WorkerError::io("sync output directory", error))
}

#[cfg(test)]
mod tests {
    use super::AtomicJsonStore;

    #[test]
    fn state_round_trip_is_atomic() {
        let root =
            std::env::temp_dir().join(format!("signal-worker-store-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let store = AtomicJsonStore::new(root.join("checkpoint.json"));
        store.save(&vec![1_u64, 2, 3]).unwrap();
        assert_eq!(store.load::<Vec<u64>>().unwrap(), Some(vec![1, 2, 3]));
        std::fs::remove_dir_all(root).unwrap();
    }
}
