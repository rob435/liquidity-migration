use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use serde::{de::DeserializeOwned, Deserialize, Serialize};
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

/// How long one frame may wait on the engine before the worker gives up on
/// it. The row is already durable, so a frame that fails costs the engine
/// only its next spool poll.
const SOCKET_WRITE_TIMEOUT: Duration = Duration::from_millis(200);

/// Delivers observations to the engine: the spool row first, then the same
/// bytes as one frame down `stream.sock` so the engine need not wait for its
/// next spool poll. The row is the delivery; the frame is the doorbell.
#[derive(Clone, Debug)]
pub struct SpoolWriter {
    directory: PathBuf,
    socket_stream: Arc<std::sync::Mutex<Option<std::os::unix::net::UnixStream>>>,
}

/// Isolated candidates live here. The engine scans `*.json` in the spool
/// directory itself, so a subdirectory is out of its delivery path.
const QUARANTINE_DIRECTORY: &str = "quarantine";
const QUARANTINE_REASON_SUFFIX: &str = ".reason";
const QUARANTINED_WITHOUT_REASON: &str = "quarantined before the reason was recorded";

/// The largest row the write path can produce: it caps the payload at
/// `MAX_SIGNAL_OBSERVATION_BYTES`, and the JSON envelope spends at most four
/// bytes per payload byte (the byte-array wire encoding) plus its bounded
/// header fields. The engine refuses a spool file over 80 MiB, so this stays
/// under that too.
const MAX_SPOOL_ROW_BYTES: u64 = 4 * MAX_SIGNAL_OBSERVATION_BYTES as u64 + 1024 * 1024;

/// Quarantine holds evidence, not backlog: a long run of bad rows or a few
/// maximal ones, an eighth of the spool's own 4,096-file, 2 GiB quota.
/// Sidecars are excluded from both.
const MAX_QUARANTINE_FILES: u64 = 256;
const MAX_QUARANTINE_BYTES: u64 = 256 * 1024 * 1024;

/// How many faulted entries the inventory names; the rest are counted only.
const MAX_REPORTED_SPOOL_FAULTS: usize = 32;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SpoolInventory {
    pub files: u64,
    pub bytes: u64,
    pub replaceable_paths: BTreeMap<String, PathBuf>,
    pub classes: BTreeMap<String, SpoolClassInventory>,
    pub quarantined_files: u64,
    pub quarantined_bytes: u64,
    /// (file name, reason) for the first `MAX_REPORTED_SPOOL_FAULTS`.
    pub quarantine_reasons: Vec<(String, String)>,
    pub unreadable_files: u64,
    pub unreadable: Vec<UnreadableSpoolEntry>,
}

/// A candidate the worker could neither read nor isolate; it still occupies
/// the spool and the engine still meets it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct UnreadableSpoolEntry {
    pub path: PathBuf,
    pub bytes: u64,
    pub reason: String,
}

/// The class key alone. A spooled row whose body no longer parses still holds
/// disk and backlog, so the inventory counts it and oldest-first trimming is
/// what removes it.
#[derive(Deserialize)]
struct SpoolEnvelope {
    kind: String,
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct SpoolClassInventory {
    pub files: u64,
    pub bytes: u64,
    pub oldest_path: Option<PathBuf>,
    pub newest_path: Option<PathBuf>,
}

#[derive(Debug, Deserialize, Serialize)]
struct QuarantineReason {
    reason: String,
    bytes: u64,
    modified_wall_ts_ms: Option<i64>,
    quarantined_wall_ts_ms: Option<i64>,
    original_path: String,
}

enum SpoolCandidate {
    Row(String),
    Vanished,
    Quarantine(String),
    Unreadable(String),
}

impl SpoolWriter {
    pub fn new(directory: impl Into<PathBuf>) -> Result<Self, WorkerError> {
        let directory = directory.into();
        fs::create_dir_all(&directory)
            .map_err(|error| WorkerError::io("create signal spool", error))?;
        cleanup_spool_temporary_files(&directory)?;
        Ok(Self {
            directory,
            socket_stream: Arc::new(std::sync::Mutex::new(None)),
        })
    }

    pub(crate) fn directory(&self) -> &Path {
        &self.directory
    }

    fn try_send_socket(&self, bytes: &[u8]) -> std::io::Result<()> {
        use std::io::Write;
        use std::os::unix::net::UnixStream;

        let sock_path = self.directory.join("stream.sock");
        let mut guard = self
            .socket_stream
            .lock()
            .map_err(|_| std::io::Error::other("socket stream mutex poisoned"))?;

        if guard.is_none() {
            if !sock_path.exists() {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::NotFound,
                    "socket file absent",
                ));
            }
            let stream = UnixStream::connect(&sock_path)?;
            stream.set_write_timeout(Some(SOCKET_WRITE_TIMEOUT))?;
            *guard = Some(stream);
        }

        if let Some(stream) = guard.as_mut() {
            // One buffer, one write: the engine reads the length and the
            // body from the same syscall's bytes.
            let mut frame = Vec::with_capacity(bytes.len().saturating_add(4));
            frame.extend_from_slice(&(bytes.len() as u32).to_le_bytes());
            frame.extend_from_slice(bytes);
            if stream.write_all(&frame).is_ok() {
                return Ok(());
            }
        }

        *guard = None;
        Err(std::io::Error::new(
            std::io::ErrorKind::BrokenPipe,
            "unix socket write failed",
        ))
    }

    pub fn write(&self, observation: &NormalizedObservation) -> Result<PathBuf, WorkerError> {
        let bytes = serde_json::to_vec(observation)
            .map_err(|error| WorkerError::json("encode signal observation", error))?;
        self.write_encoded(&bytes)
    }

    /// The deliverable backlog, plus what the scan had to set aside. One
    /// entry's content never fails the scan: a candidate the engine cannot
    /// read is isolated under `quarantine/` and reported, because the worker
    /// opens its spool on every start and the engine polls the same directory.
    pub fn inventory(&self) -> Result<SpoolInventory, WorkerError> {
        let mut inventory = SpoolInventory::default();
        self.scan_quarantine(&mut inventory)?;
        for entry in fs::read_dir(&self.directory)
            .map_err(|error| WorkerError::io("scan signal spool", error))?
        {
            let entry = entry.map_err(|error| WorkerError::io("read signal spool entry", error))?;
            let path = entry.path();
            if path.extension().is_none_or(|extension| extension != "json")
                || matches!(
                    path.file_name().and_then(|name| name.to_str()),
                    Some("input-readiness-request.json" | "input-readiness-response.json")
                )
            {
                continue;
            }
            let metadata = match fs::metadata(&path) {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
                Err(error) => {
                    report_unreadable(&mut inventory, &path, 0, format!("cannot stat: {error}"));
                    continue;
                }
            };
            if !metadata.is_file() {
                continue;
            }
            match classify_candidate(&path, &metadata) {
                SpoolCandidate::Vanished => continue,
                SpoolCandidate::Quarantine(reason) => {
                    self.quarantine(&mut inventory, &path, &metadata, reason);
                }
                SpoolCandidate::Unreadable(reason) => {
                    report_unreadable(&mut inventory, &path, metadata.len(), reason);
                }
                SpoolCandidate::Row(kind) => {
                    inventory.files = inventory.files.saturating_add(1);
                    inventory.bytes = inventory.bytes.saturating_add(metadata.len());
                    let class_inventory = inventory
                        .classes
                        .entry(spool_class(&kind).to_owned())
                        .or_default();
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
                        kind.as_str(),
                        "market_snapshot"
                            | "readiness"
                            | "long_feature_batch"
                            | "carry_feature_batch"
                    ) {
                        let pending = inventory.replaceable_paths.entry(kind).or_default();
                        if pending.as_os_str().is_empty() || path > *pending {
                            *pending = path;
                        }
                    }
                }
            }
        }
        Ok(inventory)
    }

    /// Counts what earlier scans isolated, and finishes a quarantine that a
    /// crash interrupted between the rename and its sidecar.
    fn scan_quarantine(&self, inventory: &mut SpoolInventory) -> Result<(), WorkerError> {
        let directory = self.directory.join(QUARANTINE_DIRECTORY);
        let entries = match fs::read_dir(&directory) {
            Ok(entries) => entries,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(WorkerError::io("scan signal spool quarantine", error)),
        };
        for entry in entries {
            let entry =
                entry.map_err(|error| WorkerError::io("read signal spool quarantine", error))?;
            let name = entry.file_name().to_string_lossy().into_owned();
            if name.starts_with('.') || name.ends_with(QUARANTINE_REASON_SUFFIX) {
                continue;
            }
            let path = entry.path();
            let Ok(metadata) = fs::metadata(&path) else {
                continue;
            };
            if !metadata.is_file() {
                continue;
            }
            inventory.quarantined_files = inventory.quarantined_files.saturating_add(1);
            inventory.quarantined_bytes =
                inventory.quarantined_bytes.saturating_add(metadata.len());
            let reason = match fs::read(reason_path(&path)) {
                Ok(bytes) => serde_json::from_slice::<QuarantineReason>(&bytes).map_or_else(
                    |_| "reason sidecar is unreadable".to_owned(),
                    |row| row.reason,
                ),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    let _ = write_quarantine_reason(
                        &path,
                        QUARANTINED_WITHOUT_REASON,
                        &metadata,
                        &path,
                    );
                    QUARANTINED_WITHOUT_REASON.to_owned()
                }
                Err(error) => format!("reason sidecar is unreadable: {error}"),
            };
            report_quarantined(inventory, &name, reason);
        }
        Ok(())
    }

    /// Renames the candidate into `quarantine/` and records why beside it.
    /// Refused at the bound, on a name already held, or on a failed rename:
    /// the candidate stays where it is and is reported unreadable instead.
    fn quarantine(
        &self,
        inventory: &mut SpoolInventory,
        path: &Path,
        metadata: &fs::Metadata,
        reason: String,
    ) {
        let bytes = metadata.len();
        let Some(name) = path.file_name().map(std::ffi::OsStr::to_owned) else {
            report_unreadable(inventory, path, bytes, reason);
            return;
        };
        let directory = self.directory.join(QUARANTINE_DIRECTORY);
        let target = directory.join(&name);
        if inventory.quarantined_files >= MAX_QUARANTINE_FILES
            || inventory.quarantined_bytes.saturating_add(bytes) > MAX_QUARANTINE_BYTES
        {
            report_unreadable(
                inventory,
                path,
                bytes,
                format!("{reason}; quarantine is full"),
            );
            return;
        }
        if target.exists() {
            report_unreadable(
                inventory,
                path,
                bytes,
                format!("{reason}; quarantine already holds this file name"),
            );
            return;
        }
        if let Err(error) = fs::create_dir_all(&directory).and_then(|()| fs::rename(path, &target))
        {
            report_unreadable(
                inventory,
                path,
                bytes,
                format!("{reason}; cannot quarantine: {error}"),
            );
            return;
        }
        let _ = sync_parent(&target);
        // A crash before the sidecar lands leaves the row without one; the
        // next scan writes it with the fallback reason.
        let _ = write_quarantine_reason(&target, &reason, metadata, path);
        inventory.quarantined_files = inventory.quarantined_files.saturating_add(1);
        inventory.quarantined_bytes = inventory.quarantined_bytes.saturating_add(bytes);
        report_quarantined(inventory, &name.to_string_lossy(), reason);
    }

    pub fn write_encoded_observation(
        &self,
        bytes: &[u8],
    ) -> Result<(PathBuf, NormalizedObservation), WorkerError> {
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
            if existing != bytes {
                return Err(WorkerError::state(format!(
                    "signal spool path {} already contains different bytes",
                    path.display()
                )));
            }
        } else {
            atomic_write(&path, bytes)?;
        }
        // Durable first. The frame is best effort: an engine that is down,
        // restarting, or slow reads the row on its next spool poll, and so
        // does a row wider than one frame.
        if bytes.len() <= MAX_SIGNAL_OBSERVATION_BYTES {
            let _ = self.try_send_socket(bytes);
        }
        Ok((path, observation))
    }

    pub fn write_encoded(&self, bytes: &[u8]) -> Result<PathBuf, WorkerError> {
        self.write_encoded_observation(bytes).map(|(path, _)| path)
    }
}

/// What the scan may conclude from one candidate's name, size and bytes.
fn classify_candidate(path: &Path, metadata: &fs::Metadata) -> SpoolCandidate {
    let named = path
        .file_name()
        .and_then(|name| name.to_str())
        .is_some_and(is_spool_row_name);
    if !named {
        // Without a sequence the row cannot be attributed to a source, so it
        // is isolated rather than assigned to one.
        return SpoolCandidate::Quarantine("unnamed".to_owned());
    }
    if metadata.len() > MAX_SPOOL_ROW_BYTES {
        return SpoolCandidate::Quarantine(format!(
            "oversized: {} bytes over the {MAX_SPOOL_ROW_BYTES} byte row bound",
            metadata.len()
        ));
    }
    let file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return SpoolCandidate::Vanished
        }
        Err(error) => return SpoolCandidate::Unreadable(format!("cannot open: {error}")),
    };
    match serde_json::from_reader::<_, SpoolEnvelope>(BufReader::new(file)) {
        Ok(envelope) => SpoolCandidate::Row(envelope.kind),
        Err(error) => match error.classify() {
            serde_json::error::Category::Data => {
                SpoolCandidate::Quarantine(format!("not a signal envelope: {error}"))
            }
            serde_json::error::Category::Io => {
                SpoolCandidate::Unreadable(format!("cannot read: {error}"))
            }
            _ => SpoolCandidate::Quarantine(format!("not valid JSON: {error}")),
        },
    }
}

/// `SpoolSignalFeed::parse_name`'s rule: 20 decimal digits, `-`, the content
/// hash, `.json`. A name the engine cannot page names no sequence.
fn is_spool_row_name(name: &str) -> bool {
    let Some(stem) = name.strip_suffix(".json") else {
        return false;
    };
    let Some((sequence, hash)) = stem.split_once('-') else {
        return false;
    };
    sequence.len() == 20 && sequence.bytes().all(|byte| byte.is_ascii_digit()) && !hash.is_empty()
}

fn report_quarantined(inventory: &mut SpoolInventory, name: &str, reason: String) {
    if inventory.quarantine_reasons.len() < MAX_REPORTED_SPOOL_FAULTS {
        inventory.quarantine_reasons.push((name.to_owned(), reason));
    }
}

fn report_unreadable(inventory: &mut SpoolInventory, path: &Path, bytes: u64, reason: String) {
    inventory.unreadable_files = inventory.unreadable_files.saturating_add(1);
    if inventory.unreadable.len() < MAX_REPORTED_SPOOL_FAULTS {
        inventory.unreadable.push(UnreadableSpoolEntry {
            path: path.to_owned(),
            bytes,
            reason,
        });
    }
}

fn reason_path(quarantined: &Path) -> PathBuf {
    let mut name = quarantined.file_name().unwrap_or_default().to_owned();
    name.push(QUARANTINE_REASON_SUFFIX);
    quarantined.with_file_name(name)
}

fn write_quarantine_reason(
    quarantined: &Path,
    reason: &str,
    metadata: &fs::Metadata,
    original: &Path,
) -> Result<(), WorkerError> {
    let record = QuarantineReason {
        reason: reason.to_owned(),
        bytes: metadata.len(),
        modified_wall_ts_ms: metadata.modified().ok().and_then(wall_ts_ms),
        quarantined_wall_ts_ms: wall_ts_ms(SystemTime::now()),
        original_path: original.display().to_string(),
    };
    let encoded = serde_json::to_vec_pretty(&record)
        .map_err(|error| WorkerError::json("encode signal spool quarantine reason", error))?;
    atomic_write(&reason_path(quarantined), &encoded)
}

fn wall_ts_ms(time: SystemTime) -> Option<i64> {
    time.duration_since(std::time::UNIX_EPOCH)
        .ok()
        .and_then(|elapsed| i64::try_from(elapsed.as_millis()).ok())
}

pub fn spool_class(kind: &str) -> &'static str {
    match kind {
        "market_snapshot"
        | "readiness"
        | "long_feature_batch"
        | "carry_feature_batch"
        | "llm_gate_candidates" => "current",
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
    use super::{
        cleanup_atomic_temporary_files, AtomicJsonStore, QuarantineReason, SpoolWriter,
        MAX_QUARANTINE_BYTES, MAX_SPOOL_ROW_BYTES, QUARANTINED_WITHOUT_REASON,
    };
    use crate::model::NormalizedObservation;
    use engine_types::{Feed, StrategyId, Subscription, SIGNAL_OBSERVATION_SCHEMA_VERSION};
    use std::path::{Path, PathBuf};

    fn observation(sequence: u64) -> Vec<u8> {
        observation_of(sequence, "funding_update")
    }

    fn observation_of(sequence: u64, kind: &str) -> Vec<u8> {
        let mut observation = NormalizedObservation {
            schema_version: SIGNAL_OBSERVATION_SCHEMA_VERSION,
            decision_fingerprint: "carry-v1".to_owned(),
            destination: StrategyId(2),
            source: "carry-worker".to_owned(),
            sequence,
            observation_id: format!("funding-{sequence}"),
            kind: kind.to_owned(),
            observed_wall_ts_ms: 10,
            available_wall_ts_ms: 11,
            subscriptions: vec![Subscription {
                symbol: "BTCUSDT".to_owned(),
                feed: Feed::Ticker,
            }],
            payload: br#"{"rate":"0.0001"}"#.to_vec(),
            content_sha256: String::new(),
        };
        observation.content_sha256 =
            crate::config::sha256_hex(&observation.canonical_envelope_bytes());
        serde_json::to_vec(&observation).unwrap()
    }

    /// The row is the delivery. It is on disk before the frame is sent, and
    /// the frame carries the same bytes behind one little-endian length.
    #[test]
    fn a_row_is_durable_before_the_engine_hears_its_frame() {
        use std::io::Read;
        use std::os::unix::net::UnixListener;

        let root =
            std::env::temp_dir().join(format!("signal-worker-spool-frame-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let listener = UnixListener::bind(root.join("stream.sock")).unwrap();
        let spool = SpoolWriter::new(&root).unwrap();

        let bytes = observation(1);
        let (path, parsed) = spool.write_encoded_observation(&bytes).unwrap();
        assert_eq!(parsed.sequence, 1);
        assert_eq!(
            std::fs::read(&path).unwrap(),
            bytes,
            "the row is the delivery"
        );

        let (mut engine, _) = listener.accept().unwrap();
        let mut frame = Vec::new();
        engine
            .set_read_timeout(Some(std::time::Duration::from_secs(2)))
            .unwrap();
        let mut chunk = [0u8; 65536];
        while frame.len() < bytes.len() + 4 {
            let read = engine.read(&mut chunk).unwrap();
            assert!(read > 0, "frame ended early");
            frame.extend_from_slice(&chunk[..read]);
        }
        assert_eq!(&frame[..4], &(bytes.len() as u32).to_le_bytes());
        assert_eq!(&frame[4..], &bytes[..]);

        // Without an engine listening the row still lands.
        drop(engine);
        drop(listener);
        let _ = std::fs::remove_file(root.join("stream.sock"));
        let second = observation(2);
        let (second_path, _) = spool.write_encoded_observation(&second).unwrap();
        assert_eq!(std::fs::read(&second_path).unwrap(), second);
        std::fs::remove_dir_all(root).unwrap();
    }

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

    /// A row wider than one frame is still the delivery; the doorbell is
    /// skipped and the engine reads the row on its next spool poll.
    #[test]
    fn a_row_wider_than_one_frame_rings_no_doorbell() {
        use std::os::unix::net::UnixListener;

        let root =
            std::env::temp_dir().join(format!("signal-worker-spool-fat-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        let listener = UnixListener::bind(root.join("stream.sock")).unwrap();
        listener.set_nonblocking(true).unwrap();
        let spool = SpoolWriter::new(&root).unwrap();

        let mut fat: NormalizedObservation = serde_json::from_slice(&observation(3)).unwrap();
        // The envelope is wider than the payload, and it is the envelope
        // that travels as one frame.
        fat.payload = vec![b'x'; engine_types::MAX_SIGNAL_OBSERVATION_BYTES - 1];
        fat.content_sha256 = crate::config::sha256_hex(&fat.canonical_envelope_bytes());
        let bytes = serde_json::to_vec(&fat).unwrap();
        assert!(fat.payload.len() < engine_types::MAX_SIGNAL_OBSERVATION_BYTES);
        assert!(bytes.len() > engine_types::MAX_SIGNAL_OBSERVATION_BYTES);

        let (path, parsed) = spool.write_encoded_observation(&bytes).unwrap();
        assert_eq!(parsed.sequence, 3);
        assert_eq!(
            std::fs::read(&path).unwrap(),
            bytes,
            "the row is the delivery"
        );
        assert_eq!(
            listener.accept().map(|_| ()).unwrap_err().kind(),
            std::io::ErrorKind::WouldBlock,
            "no frame was offered for the fat row"
        );

        drop(listener);
        std::fs::remove_dir_all(root).unwrap();
    }

    fn spool_root(label: &str) -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "signal-worker-spool-{label}-{}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        std::fs::create_dir_all(&root).unwrap();
        root
    }

    fn row_name(sequence: u64, hash_byte: &str) -> String {
        format!("{sequence:020}-{}.json", hash_byte.repeat(32))
    }

    fn quarantine_reason(root: &Path, name: &str) -> QuarantineReason {
        let bytes = std::fs::read(root.join("quarantine").join(format!("{name}.reason"))).unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }

    fn sparse_file(path: &Path, len: u64) {
        std::fs::File::create(path).unwrap().set_len(len).unwrap();
    }

    #[test]
    fn a_spooled_row_with_an_unreadable_body_still_counts_toward_its_class() {
        let root = spool_root("envelope");
        let spool = SpoolWriter::new(&root).unwrap();

        let good = root.join("00000000000000000001-good.json");
        std::fs::write(&good, observation(1)).unwrap();
        let damaged = root.join("00000000000000000002-damaged.json");
        std::fs::write(
            &damaged,
            br#"{"kind":"funding_update","payload_wire":"not-base64","sequence":"two"}"#,
        )
        .unwrap();

        let inventory = spool.inventory().unwrap();
        assert_eq!(inventory.files, 2);
        assert_eq!(
            inventory.bytes,
            std::fs::metadata(&good).unwrap().len() + std::fs::metadata(&damaged).unwrap().len()
        );
        let lifecycle = &inventory.classes["lifecycle"];
        assert_eq!(lifecycle.files, 2);
        assert_eq!(lifecycle.oldest_path.as_deref(), Some(good.as_path()));
        assert_eq!(lifecycle.newest_path.as_deref(), Some(damaged.as_path()));
        std::fs::remove_dir_all(root).unwrap();
    }

    /// One unreadable row used to fail the whole scan, so `Durable::open`
    /// failed and systemd restarted into the same file.
    #[test]
    fn a_malformed_row_does_not_fail_the_scan() {
        let root = spool_root("malformed");
        let spool = SpoolWriter::new(&root).unwrap();
        spool.write_encoded(&observation(1)).unwrap();
        let truncated = row_name(2, "ab");
        std::fs::write(
            root.join(&truncated),
            br#"{"schema_version":1,"kind":"fund"#,
        )
        .unwrap();

        let inventory = spool.inventory().unwrap();
        assert_eq!(inventory.files, 1, "the readable row is still backlog");
        assert_eq!(inventory.quarantined_files, 1);
        assert_eq!(inventory.unreadable_files, 0);
        assert!(!root.join(&truncated).exists());
        assert!(root.join("quarantine").join(&truncated).exists());
        assert!(
            quarantine_reason(&root, &truncated)
                .reason
                .starts_with("not valid JSON: "),
            "{:?}",
            inventory.quarantine_reasons
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_row_without_a_readable_kind_is_quarantined() {
        let root = spool_root("no-kind");
        let spool = SpoolWriter::new(&root).unwrap();
        let kindless = "00000000000000000001-kindless.json";
        std::fs::write(root.join(kindless), br#"{"sequence":1}"#).unwrap();

        let inventory = spool.inventory().unwrap();
        assert_eq!((inventory.files, inventory.quarantined_files), (0, 1));
        assert_eq!(
            quarantine_reason(&root, kindless).reason,
            "not a signal envelope: missing field `kind` at line 1 column 14"
        );
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_row_over_the_row_bound_is_quarantined_unread() {
        let root = spool_root("oversized");
        let spool = SpoolWriter::new(&root).unwrap();
        let oversized = row_name(4, "ef");
        sparse_file(&root.join(&oversized), MAX_SPOOL_ROW_BYTES + 1);

        let inventory = spool.inventory().unwrap();
        assert_eq!((inventory.files, inventory.quarantined_files), (0, 1));
        assert_eq!(inventory.quarantined_bytes, MAX_SPOOL_ROW_BYTES + 1);
        let reason = quarantine_reason(&root, &oversized);
        assert!(reason.reason.starts_with("oversized: "), "{reason:?}");
        assert_eq!(reason.bytes, MAX_SPOOL_ROW_BYTES + 1);
        std::fs::remove_dir_all(root).unwrap();
    }

    /// The neighbours of a bad row stay deliverable, including the coalescing
    /// path the worker replaces rather than appends.
    #[test]
    fn a_bad_row_between_two_valid_sequences_keeps_the_valid_ones() {
        let root = spool_root("neighbours");
        let spool = SpoolWriter::new(&root).unwrap();
        let first = spool
            .write_encoded(&observation_of(1, "market_snapshot"))
            .unwrap();
        let bad = row_name(2, "ab");
        std::fs::write(root.join(&bad), b"{").unwrap();
        let third = spool
            .write_encoded(&observation_of(3, "market_snapshot"))
            .unwrap();

        let inventory = spool.inventory().unwrap();
        assert_eq!(inventory.files, 2);
        assert_eq!(inventory.classes["current"].files, 2);
        assert_eq!(
            inventory.classes["current"].oldest_path.as_deref(),
            Some(first.as_path())
        );
        assert_eq!(
            inventory.replaceable_paths["market_snapshot"].as_path(),
            third.as_path()
        );
        assert_eq!(inventory.quarantined_files, 1);
        assert_eq!(inventory.quarantine_reasons.len(), 1);
        assert_eq!(inventory.quarantine_reasons[0].0, bad);
        let reason = quarantine_reason(&root, &bad);
        assert_eq!(reason.bytes, 1);
        assert_eq!(reason.original_path, root.join(&bad).display().to_string());
        assert!(reason.modified_wall_ts_ms.is_some());
        assert!(reason.quarantined_wall_ts_ms.is_some());
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_file_whose_name_holds_no_sequence_is_quarantined() {
        let root = spool_root("unnamed");
        let spool = SpoolWriter::new(&root).unwrap();
        std::fs::write(root.join("notes.json"), br#"{"kind":"funding_update"}"#).unwrap();

        let inventory = spool.inventory().unwrap();
        assert_eq!((inventory.files, inventory.quarantined_files), (0, 1));
        assert_eq!(quarantine_reason(&root, "notes.json").reason, "unnamed");
        std::fs::remove_dir_all(root).unwrap();
    }

    /// At the bound the worker keeps the evidence it already holds and says
    /// it could not isolate the rest.
    #[test]
    fn a_full_quarantine_leaves_the_entry_in_place_and_reports_it() {
        let root = spool_root("quarantine-full");
        let spool = SpoolWriter::new(&root).unwrap();
        let half = MAX_QUARANTINE_BYTES / 2 + 1;
        let first = row_name(5, "ab");
        let second = row_name(6, "cd");
        sparse_file(&root.join(&first), half);
        sparse_file(&root.join(&second), half);

        let inventory = spool.inventory().unwrap();
        assert_eq!(inventory.quarantined_files, 1);
        assert_eq!(inventory.quarantined_bytes, half);
        assert_eq!(inventory.unreadable_files, 1);
        assert_eq!(inventory.unreadable[0].path, root.join(&second));
        assert_eq!(inventory.unreadable[0].bytes, half);
        assert!(
            inventory.unreadable[0]
                .reason
                .ends_with("quarantine is full"),
            "{}",
            inventory.unreadable[0].reason
        );
        assert!(root.join(&second).exists(), "nothing is deleted");
        std::fs::remove_dir_all(root).unwrap();
    }

    /// A row retired by the engine between `read_dir` and the scan's own look
    /// is not backlog and not a fault.
    #[test]
    fn an_entry_that_no_longer_resolves_is_skipped() {
        let root = spool_root("vanished");
        let spool = SpoolWriter::new(&root).unwrap();
        spool.write_encoded(&observation(1)).unwrap();
        let vanished = row_name(2, "ab");
        std::os::unix::fs::symlink(root.join("gone.json"), root.join(&vanished)).unwrap();

        let inventory = spool.inventory().unwrap();
        assert_eq!(inventory.files, 1);
        assert_eq!(inventory.quarantined_files, 0);
        assert_eq!(inventory.unreadable_files, 0);
        std::fs::remove_dir_all(root).unwrap();
    }

    /// A crash between the rename and the sidecar leaves evidence without its
    /// reason; the next scan records what it can still say.
    #[test]
    fn a_quarantined_row_without_a_sidecar_gets_one() {
        let root = spool_root("sidecarless");
        let spool = SpoolWriter::new(&root).unwrap();
        let orphan = row_name(7, "ab");
        std::fs::create_dir_all(root.join("quarantine")).unwrap();
        std::fs::write(root.join("quarantine").join(&orphan), b"{").unwrap();

        let inventory = spool.inventory().unwrap();
        assert_eq!(inventory.quarantined_files, 1);
        assert_eq!(inventory.quarantined_bytes, 1);
        assert_eq!(
            inventory.quarantine_reasons,
            vec![(orphan.clone(), QUARANTINED_WITHOUT_REASON.to_owned())]
        );
        let reason = quarantine_reason(&root, &orphan);
        assert_eq!(reason.reason, QUARANTINED_WITHOUT_REASON);
        assert_eq!(reason.bytes, 1);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn repeated_scans_report_the_same_quarantine() {
        let root = spool_root("idempotent");
        let spool = SpoolWriter::new(&root).unwrap();
        spool.write_encoded(&observation(1)).unwrap();
        let bad = row_name(2, "ab");
        std::fs::write(root.join(&bad), b"{").unwrap();

        let first = spool.inventory().unwrap();
        let second = spool.inventory().unwrap();
        assert_eq!(first, second);
        assert_eq!(second.files, 1);
        assert_eq!(second.quarantined_files, 1);
        assert_eq!(second.unreadable_files, 0);
        assert_eq!(
            std::fs::read_dir(root.join("quarantine")).unwrap().count(),
            2,
            "the row and its one sidecar"
        );
        std::fs::remove_dir_all(root).unwrap();
    }
}
