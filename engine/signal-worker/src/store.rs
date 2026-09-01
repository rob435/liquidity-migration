use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime};

use serde::{de::DeserializeOwned, Serialize};
use sha2::{Digest, Sha256};

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
        match File::open(&self.path) {
            Ok(file) => serde_json::from_reader(BufReader::new(file))
                .map(Some)
                .map_err(|error| WorkerError::json("parse durable state", error)),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(WorkerError::io("read durable state", error)),
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
        atomic_write_json(&self.path, value)
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

    pub fn replace_from(&self, source: &Self) -> Result<(), WorkerError> {
        fs::rename(&source.path, &self.path)
            .map_err(|error| WorkerError::io("publish durable state", error))?;
        sync_parent(&self.path)
    }

    pub fn sha256(&self) -> Result<Option<String>, WorkerError> {
        let mut file = match File::open(&self.path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(WorkerError::io("open durable state for hashing", error)),
        };
        let mut hasher = Sha256::new();
        let mut buffer = [0_u8; 64 * 1024];
        loop {
            let read = file
                .read(&mut buffer)
                .map_err(|error| WorkerError::io("hash durable state", error))?;
            if read == 0 {
                break;
            }
            hasher.update(&buffer[..read]);
        }
        Ok(Some(hex::encode(hasher.finalize())))
    }

    pub fn len(&self) -> Result<u64, WorkerError> {
        match fs::metadata(&self.path) {
            Ok(metadata) => Ok(metadata.len()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(0),
            Err(error) => Err(WorkerError::io("stat durable state", error)),
        }
    }

    pub fn is_empty(&self) -> Result<bool, WorkerError> {
        Ok(self.len()? == 0)
    }

    pub fn age(&self) -> Result<Option<Duration>, WorkerError> {
        match fs::metadata(&self.path) {
            Ok(metadata) => {
                let modified = metadata
                    .modified()
                    .map_err(|error| WorkerError::io("read durable state clock", error))?;
                Ok(Some(
                    SystemTime::now()
                        .duration_since(modified)
                        .unwrap_or(Duration::ZERO),
                ))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(WorkerError::io("stat durable state", error)),
        }
    }
}

#[derive(Clone, Debug)]
pub struct AppendJournal {
    path: PathBuf,
}

impl AppendJournal {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self { path: path.into() }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append<T: Serialize>(&self, value: &T) -> Result<(), WorkerError> {
        let parent = self
            .path
            .parent()
            .ok_or_else(|| WorkerError::state("journal path has no parent"))?;
        fs::create_dir_all(parent)
            .map_err(|error| WorkerError::io("create journal directory", error))?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|error| WorkerError::io("open input journal", error))?;
        serde_json::to_writer(&mut file, value)
            .map_err(|error| WorkerError::json("encode input journal entry", error))?;
        finish_journal_append(&self.path, file)
    }

    pub fn append_bytes(&self, bytes: &[u8]) -> Result<(), WorkerError> {
        serde_json::from_slice::<serde_json::Value>(bytes)
            .map_err(|error| WorkerError::json("validate input journal entry", error))?;
        let parent = self
            .path
            .parent()
            .ok_or_else(|| WorkerError::state("journal path has no parent"))?;
        fs::create_dir_all(parent)
            .map_err(|error| WorkerError::io("create journal directory", error))?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|error| WorkerError::io("open input journal", error))?;
        file.write_all(bytes)
            .map_err(|error| WorkerError::io("write input journal entry", error))?;
        finish_journal_append(&self.path, file)
    }

    pub fn replay<T, F>(&self, mut visit: F) -> Result<u64, WorkerError>
    where
        T: DeserializeOwned,
        F: FnMut(T) -> Result<(), WorkerError>,
    {
        let file = match OpenOptions::new().read(true).write(true).open(&self.path) {
            Ok(file) => file,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(0),
            Err(error) => return Err(WorkerError::io("open input journal", error)),
        };
        let mut reader = BufReader::new(file);
        let mut frame = Vec::new();
        let mut durable_len = 0_u64;
        let mut entries = 0_u64;
        loop {
            frame.clear();
            let read = reader
                .read_until(b'\n', &mut frame)
                .map_err(|error| WorkerError::io("read input journal", error))?;
            if read == 0 {
                break;
            }
            if frame.last() != Some(&b'\n') {
                reader
                    .get_mut()
                    .set_len(durable_len)
                    .map_err(|error| WorkerError::io("truncate incomplete journal tail", error))?;
                reader
                    .get_mut()
                    .sync_all()
                    .map_err(|error| WorkerError::io("sync repaired input journal", error))?;
                break;
            }
            let entry = serde_json::from_slice(&frame[..frame.len() - 1])
                .map_err(|error| WorkerError::json("parse input journal entry", error))?;
            visit(entry)?;
            durable_len = durable_len.saturating_add(read as u64);
            entries = entries.saturating_add(1);
        }
        Ok(entries)
    }

    pub fn remove(&self) -> Result<(), WorkerError> {
        match fs::remove_file(&self.path) {
            Ok(()) => sync_parent(&self.path),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(WorkerError::io("remove input journal", error)),
        }
    }

    pub fn len(&self) -> Result<u64, WorkerError> {
        match fs::metadata(&self.path) {
            Ok(metadata) => Ok(metadata.len()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(0),
            Err(error) => Err(WorkerError::io("stat input journal", error)),
        }
    }

    pub fn is_empty(&self) -> Result<bool, WorkerError> {
        Ok(self.len()? == 0)
    }
}

pub fn json_size<T: Serialize>(value: &T) -> Result<u64, WorkerError> {
    struct Counter(u64);

    impl Write for Counter {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0 = self.0.saturating_add(bytes.len() as u64);
            Ok(bytes.len())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    let mut counter = Counter(0);
    serde_json::to_writer(&mut counter, value)
        .map_err(|error| WorkerError::json("measure input journal entry", error))?;
    Ok(counter.0)
}

fn finish_journal_append(path: &Path, mut file: File) -> Result<(), WorkerError> {
    file.write_all(b"\n")
        .map_err(|error| WorkerError::io("finish input journal entry", error))?;
    file.sync_all()
        .map_err(|error| WorkerError::io("sync input journal", error))?;
    sync_parent(path)
}

#[derive(Clone, Debug)]
pub struct SpoolWriter {
    directory: PathBuf,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SpoolInventory {
    pub files: u64,
    pub bytes: u64,
    pub replaceable_paths: BTreeMap<String, PathBuf>,
    pub classes: BTreeMap<String, SpoolClassInventory>,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SpoolClassInventory {
    pub files: u64,
    pub bytes: u64,
    pub oldest_path: Option<PathBuf>,
    pub newest_path: Option<PathBuf>,
}

impl SpoolWriter {
    pub fn new(directory: impl Into<PathBuf>) -> Result<Self, WorkerError> {
        let directory = directory.into();
        fs::create_dir_all(&directory)
            .map_err(|error| WorkerError::io("create signal spool", error))?;
        cleanup_spool_temporary_files(&directory)?;
        Ok(Self { directory })
    }

    pub fn write(&self, observation: &NormalizedObservation) -> Result<PathBuf, WorkerError> {
        let bytes = serde_json::to_vec(observation)
            .map_err(|error| WorkerError::json("encode signal observation", error))?;
        self.write_encoded(&bytes)
    }

    pub fn inventory(&self) -> Result<SpoolInventory, WorkerError> {
        let mut inventory = SpoolInventory::default();
        for entry in fs::read_dir(&self.directory)
            .map_err(|error| WorkerError::io("scan signal spool", error))?
        {
            let entry = entry.map_err(|error| WorkerError::io("read signal spool entry", error))?;
            let path = entry.path();
            if !path
                .extension()
                .is_some_and(|extension| extension == "json")
            {
                continue;
            }
            let metadata = match entry.metadata() {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    return Err(WorkerError::io("inspect signal spool entry", error));
                }
            };
            if !metadata.is_file() {
                continue;
            }
            let file = match File::open(&path) {
                Ok(file) => file,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => return Err(WorkerError::io("open signal spool entry", error)),
            };
            let observation: NormalizedObservation = serde_json::from_reader(BufReader::new(file))
                .map_err(|error| WorkerError::json("parse signal spool inventory", error))?;
            inventory.files = inventory.files.saturating_add(1);
            inventory.bytes = inventory.bytes.saturating_add(metadata.len());
            let class = spool_class(&observation.kind).to_owned();
            let class_inventory = inventory.classes.entry(class).or_default();
            class_inventory.files = class_inventory.files.saturating_add(1);
            class_inventory.bytes = class_inventory.bytes.saturating_add(metadata.len());
            if class_inventory
                .oldest_path
                .as_ref()
                .is_none_or(|oldest| path < *oldest)
            {
                class_inventory.oldest_path = Some(path.clone());
            }
            if class_inventory
                .newest_path
                .as_ref()
                .is_none_or(|newest| path > *newest)
            {
                class_inventory.newest_path = Some(path.clone());
            }
            if matches!(
                observation.kind.as_str(),
                "market_snapshot" | "readiness" | "long_feature_batch" | "carry_feature_batch"
            ) {
                let pending = inventory
                    .replaceable_paths
                    .entry(observation.kind)
                    .or_default();
                if pending.as_os_str().is_empty() || path > *pending {
                    *pending = path;
                }
            }
        }
        Ok(inventory)
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

pub fn spool_class(kind: &str) -> &'static str {
    match kind {
        "market_snapshot" | "readiness" | "long_feature_batch" | "carry_feature_batch" => "current",
        "funding_update" => "lifecycle",
        "carry_scorer_catchup" => "catchup",
        _ => "other",
    }
}

fn cleanup_spool_temporary_files(directory: &Path) -> Result<(), WorkerError> {
    for entry in fs::read_dir(directory)
        .map_err(|error| WorkerError::io("scan signal spool temporary files", error))?
    {
        let entry =
            entry.map_err(|error| WorkerError::io("read signal spool temporary entry", error))?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        if !name.starts_with('.') || !name.ends_with(".tmp") || !name.contains(".json.") {
            continue;
        }
        let file_type = match entry.file_type() {
            Ok(file_type) => file_type,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(WorkerError::io("inspect signal spool temporary", error)),
        };
        if !file_type.is_file() {
            continue;
        }
        match fs::remove_file(entry.path()) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(WorkerError::io("remove signal spool temporary", error)),
        }
    }
    Ok(())
}

pub fn cleanup_atomic_temporary_files(
    directory: &Path,
    target_names: &[&str],
) -> Result<(), WorkerError> {
    if !directory.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(directory)
        .map_err(|error| WorkerError::io("scan durable temporary files", error))?
    {
        let entry =
            entry.map_err(|error| WorkerError::io("read durable temporary entry", error))?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let owned = target_names
            .iter()
            .any(|target| name.starts_with(&format!(".{target}.")) && name.ends_with(".tmp"));
        if !owned {
            continue;
        }
        let file_type = match entry.file_type() {
            Ok(file_type) => file_type,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(WorkerError::io("inspect durable temporary", error)),
        };
        if !file_type.is_file() {
            continue;
        }
        match fs::remove_file(entry.path()) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(WorkerError::io("remove durable temporary", error)),
        }
    }
    Ok(())
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

fn atomic_write_json<T: Serialize>(path: &Path, value: &T) -> Result<(), WorkerError> {
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
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|error| WorkerError::io("create atomic temporary file", error))?;
    let result = (|| {
        let mut writer = std::io::BufWriter::new(file);
        serde_json::to_writer(&mut writer, value)
            .map_err(|error| WorkerError::json("encode durable state", error))?;
        writer
            .flush()
            .map_err(|error| WorkerError::io("flush atomic temporary file", error))?;
        writer
            .get_ref()
            .sync_all()
            .map_err(|error| WorkerError::io("sync atomic temporary file", error))?;
        drop(writer);
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
    use super::{cleanup_atomic_temporary_files, AtomicJsonStore, SpoolWriter};

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

    #[test]
    fn recovery_removes_only_owned_atomic_temporary_files() {
        let root =
            std::env::temp_dir().join(format!("signal-worker-temp-cleanup-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let owned = root.join(".checkpoint.json.123-1.tmp");
        let unrelated = root.join(".someone-else.json.123-1.tmp");
        std::fs::write(&owned, b"partial").unwrap();
        std::fs::write(&unrelated, b"keep").unwrap();
        cleanup_atomic_temporary_files(&root, &["checkpoint.json"]).unwrap();
        assert!(!owned.exists());
        assert!(unrelated.exists());

        let spool = root.join("spool");
        std::fs::create_dir_all(&spool).unwrap();
        let orphan = spool.join(".00000000000000000001-hash.json.123-1.tmp");
        std::fs::write(&orphan, b"partial").unwrap();
        SpoolWriter::new(&spool).unwrap();
        assert!(!orphan.exists());
        std::fs::remove_dir_all(root).unwrap();
    }
}
