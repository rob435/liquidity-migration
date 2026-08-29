//! The engine's append-only log.
//!
//! Layout: an 8-byte magic header, then frames. One frame is
//! `[u32 LE payload length][u32 LE crc32c of the payload][payload]`, and the
//! payload is one [`WalRecord`] as JSON. Sequence numbers are not stored on
//! disk: a record's sequence is its frame index, counting from 1.
//!
//! One writer — [`lock`] is how that is made true rather than assumed.
//! Appends are buffered; [`Wal::flush`] hands the bytes to the OS,
//! [`Wal::barrier`] also waits for the disk and is the durability point used
//! before an order leaves the socket.
//!
//! On open the log is replayed. A tail that did not survive a crash — a short
//! frame, a frame that runs past the end of the file, or one whose checksum
//! does not match — is cut off, and appending resumes on that boundary. A bad
//! header is refused instead: a file that is not ours is not ours to truncate.
//!
//! ## Segments
//!
//! A log that grows without bound is rotated into numbered segments in the
//! same directory: the configured path is segment 1, and rotation adds
//! `<name>.000002`, `<name>.000003`, … Every segment after the first begins
//! with one [`WalRecord::SegmentBase`] restating everything boot replay
//! needs, so boot replays only the newest segment it can trust —
//! [`open_current`] — and the older ones are archives, never deleted by any
//! code here. Retention is the owner's decision. [`replay_chain`] reads the
//! whole family in order for the offline tools. The single-writer claim
//! stays on the configured path ([`lock`]), so one flock covers every
//! segment and the rotation between them.

use std::ffi::CString;
use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, Read, Seek, SeekFrom, Write};
use std::os::unix::ffi::OsStrExt;
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::Instant;

pub use engine_types::wal::{PendingBarrier, Wal, WalError, WalRecord};

/// Magic at offset 0. The trailing digits are the format version.
const MAGIC: [u8; 8] = *b"EWAL0001";
const HEADER_LEN: u64 = 8;
const FRAME_HEADER_LEN: usize = 8;

/// Buffered bytes go to the OS once they pass this, so a quiet log never
/// grows an unbounded buffer.
const BUFFER_HIGH_WATER: usize = 64 * 1024;

/// The append-only log writer.
/// The thread that waits on the disk, so the order path does not have to.
///
/// It holds its own descriptor for the same file — `try_clone` shares the open
/// file description, so a barrier it runs covers every byte the writer has
/// already handed to the operating system. Nothing else is shared: the writer
/// keeps sole ownership of writing and of the buffer.
///
/// One thread for the life of a segment. A barrier is a few hundred a second
/// at most, and spawning one thread each time would cost a meaningful part of
/// what this exists to save.
struct SyncThread {
    /// Each request carries the channel its answer goes back on.
    requests: mpsc::Sender<mpsc::Sender<Result<(), WalError>>>,
    /// The file this thread's descriptor was opened on. A barrier syncs the
    /// file, not the path, so a thread left over from before a rotation would
    /// pass every barrier while saying nothing about the segment being
    /// written — and nothing else about that is observable, because the bytes
    /// are readable from the page cache either way. Read only by the test
    /// that holds the invariant; it exists to make it checkable at all.
    #[cfg_attr(not(test), allow(dead_code))]
    inode: u64,
}

impl SyncThread {
    fn spawn(file: &File) -> Result<Self, WalError> {
        use std::os::unix::fs::MetadataExt;
        let inode = file.metadata()?.ino();
        let fd = file.try_clone()?;
        let (requests, incoming) = mpsc::channel::<mpsc::Sender<Result<(), WalError>>>();
        std::thread::Builder::new()
            .name("wal-sync".to_string())
            .spawn(move || {
                for reply in incoming {
                    // A receiver that has gone away is a caller that stopped
                    // caring; the barrier still ran.
                    let _ = reply.send(fd.sync_data().map_err(WalError::Io));
                }
            })?;
        Ok(SyncThread { requests, inode })
    }

    /// Ask for a barrier. `None` when the thread is gone, which the caller
    /// answers by running the barrier itself rather than by carrying on.
    fn request(&self) -> Option<mpsc::Receiver<Result<(), WalError>>> {
        let (reply, done) = mpsc::channel();
        self.requests.send(reply).ok().map(|()| done)
    }
}

pub struct WalWriter {
    file: File,
    /// Absent only for a writer whose thread could not be started, which
    /// falls back to synchronous barriers rather than to none.
    sync: Option<SyncThread>,
    /// Frames waiting to go to the OS. Reused across appends: a record is
    /// serialized straight into it, so a warm writer allocates nothing.
    buf: Vec<u8>,
    next_seq: u64,
    /// The configured log path — segment 1, and the name every later
    /// segment's number is appended to.
    family: PathBuf,
    /// Bytes already in the current segment's file (header included).
    file_bytes: u64,
}

impl WalWriter {
    /// Open an existing log or create a new one, replay what is on disk, and
    /// position for appending after the last good frame. Returns the writer
    /// and every record replayed, each with its sequence.
    ///
    /// This opens exactly the file named. A log that may have been rotated is
    /// opened with [`open_current`], which picks the newest segment boot can
    /// trust.
    pub fn open(path: impl AsRef<Path>) -> Result<(Self, Vec<(u64, WalRecord)>), WalError> {
        Self::open_segment(path.as_ref(), path.as_ref())
    }

    fn open_segment(path: &Path, family: &Path) -> Result<(Self, Vec<(u64, WalRecord)>), WalError> {
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        let len = file.metadata()?.len();

        let scan = if len == 0 {
            file.write_all(&MAGIC)?;
            file.sync_data()?;
            if let Some(dir) = path.parent() {
                File::open(dir)?.sync_all()?;
            }
            Scan {
                records: Vec::new(),
                good_end: HEADER_LEN,
            }
        } else {
            scan_file(&mut file, len)?
        };

        if scan.good_end < len {
            file.set_len(scan.good_end)?;
        }
        file.seek(SeekFrom::Start(scan.good_end))?;

        let next_seq = scan.records.len() as u64 + 1;
        let sync = SyncThread::spawn(&file).ok();
        let writer = WalWriter {
            file,
            sync,
            buf: Vec::with_capacity(BUFFER_HIGH_WATER),
            next_seq,
            family: family.to_path_buf(),
            file_bytes: scan.good_end,
        };
        Ok((writer, scan.records))
    }

    /// The sequence the next append will get.
    pub fn next_seq(&self) -> u64 {
        self.next_seq
    }

    /// The file the durability thread would sync, and the file being written.
    /// Equal, or a barrier proves nothing. See [`SyncThread::inode`].
    #[cfg(test)]
    fn sync_and_write_inodes(&self) -> (Option<u64>, u64) {
        use std::os::unix::fs::MetadataExt;
        (
            self.sync.as_ref().map(|sync| sync.inode),
            self.file.metadata().expect("the open segment").ino(),
        )
    }

    fn push_to_os(&mut self) -> Result<(), WalError> {
        if self.buf.is_empty() {
            return Ok(());
        }
        self.file.write_all(&self.buf)?;
        self.file_bytes += self.buf.len() as u64;
        self.buf.clear();
        Ok(())
    }
}

/// A frame the log could not read back is not a frame.
///
/// An `f64` that is not a number is written as `null` — serde_json's own answer
/// for one — and then no reader can turn that record back into a record.
/// Nothing else this log holds can do that, so a payload with no `null` in it
/// needs no check; one with a `null` in it may be an absent `Option` and may be
/// a number that is not a number, and reading it back is the only way to tell.
///
/// It matters because the reader refuses rather than truncates: bytes that pass
/// their checksum are real data, and deleting them is not the log's call. So one
/// such record makes the whole log unopenable, at the next boot, for good.
fn reads_back(payload: &[u8]) -> Result<(), WalError> {
    if !payload.windows(4).any(|window| window == b"null") {
        return Ok(());
    }
    serde_json::from_slice::<WalRecord>(payload)
        .map(|_: WalRecord| ())
        .map_err(|e| {
            WalError::Io(io::Error::new(
                io::ErrorKind::InvalidData,
                format!("this record does not read back, so it is not written: {e}"),
            ))
        })
}

impl Wal for WalWriter {
    fn append(&mut self, record: &WalRecord) -> Result<u64, WalError> {
        let start = self.buf.len();
        self.buf.extend_from_slice(&[0u8; FRAME_HEADER_LEN]);
        if let Err(e) = serde_json::to_writer(&mut self.buf, record) {
            // A half-written record must not become a frame.
            self.buf.truncate(start);
            return Err(WalError::Io(io::Error::new(io::ErrorKind::InvalidData, e)));
        }

        if let Err(e) = reads_back(&self.buf[start + FRAME_HEADER_LEN..]) {
            self.buf.truncate(start);
            return Err(e);
        }

        let payload = &self.buf[start + FRAME_HEADER_LEN..];
        let payload_len = payload.len() as u32;
        let crc = crc32c::crc32c(payload);
        self.buf[start..start + 4].copy_from_slice(&payload_len.to_le_bytes());
        self.buf[start + 4..start + FRAME_HEADER_LEN].copy_from_slice(&crc.to_le_bytes());

        let seq = self.next_seq;
        self.next_seq += 1;
        if self.buf.len() >= BUFFER_HIGH_WATER {
            self.push_to_os()?;
        }
        Ok(seq)
    }

    fn barrier(&mut self) -> Result<(), WalError> {
        self.push_to_os()?;
        self.file.sync_data()?;
        Ok(())
    }

    fn barrier_begin(&mut self) -> Result<PendingBarrier, WalError> {
        // The write happens here, on the writer, before the request goes out.
        // That is what makes the barrier cover these bytes: the sync thread
        // shares the file, not the buffer, so anything still in the buffer
        // when it runs would not be covered by it.
        self.push_to_os()?;
        match self.sync.as_ref().and_then(SyncThread::request) {
            Some(done) => Ok(PendingBarrier::running(done)),
            // No thread to ask. Do it here rather than hand back a promise
            // nothing is keeping.
            None => {
                self.file.sync_data()?;
                Ok(PendingBarrier::settled())
            }
        }
    }

    fn flush(&mut self) -> Result<(), WalError> {
        self.push_to_os()
    }

    fn segment_size(&self) -> u64 {
        self.file_bytes + self.buf.len() as u64
    }

    /// Start the next segment, first record `base`, and archive the current
    /// one in place. Nothing is ever deleted here — retention is the owner's.
    ///
    /// The crash-ordering argument, step by step. A crash at ANY point must
    /// leave [`open_current`] recovering the same engine:
    ///
    /// 1. The old segment's buffered tail is pushed and fdatasync'd, so the
    ///    archive is complete before anything new exists. A crash here: the
    ///    new segment does not exist, boot replays the old one. Nothing lost.
    /// 2. The new segment is created under a name no segment has used
    ///    (`create_new`, at one past the highest number in the directory —
    ///    an abandoned torn segment from an earlier crash is skipped, never
    ///    reused, because appending a fresh restatement after half an old one
    ///    would replay both). A crash while its base record is being written
    ///    leaves a torn first frame: [`open_current`] refuses a numbered
    ///    segment whose first record does not read back as a complete
    ///    `SegmentBase` — the frame checksum makes that one mechanical check
    ///    — and falls back to the old segment. Nothing lost, nothing
    ///    invented.
    /// 3. The base frame is fdatasync'd, then the directory entry is
    ///    fsync'd. A crash after both but before this writer switches: the
    ///    new segment IS trusted and boot may pick it — which recovers the
    ///    identical state, because the base restates the old segment in
    ///    full and nothing has been appended anywhere since it was computed
    ///    (the caller is the single-threaded engine loop).
    /// 4. Only then does this writer switch its file handle. From the first
    ///    append after that, the new segment is the one with unique records,
    ///    and it is already the one boot picks.
    fn rotate(&mut self, base: &WalRecord) -> Result<bool, WalError> {
        // 1. Finish the archive.
        self.push_to_os()?;
        self.file.sync_data()?;

        // 2. The next unused number, torn leftovers included.
        let next_index = segments(&self.family)?
            .last()
            .map(|(index, _)| index + 1)
            .unwrap_or(2)
            .max(2);
        let path = segment_path(&self.family, next_index);
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create_new(true)
            .open(&path)?;
        file.write_all(&MAGIC)?;
        let mut frame: Vec<u8> = Vec::with_capacity(BUFFER_HIGH_WATER);
        frame.extend_from_slice(&[0u8; FRAME_HEADER_LEN]);
        serde_json::to_writer(&mut frame, base)
            .map_err(|e| WalError::Io(io::Error::new(io::ErrorKind::InvalidData, e)))?;
        reads_back(&frame[FRAME_HEADER_LEN..])?;
        let payload = &frame[FRAME_HEADER_LEN..];
        let payload_len = payload.len() as u32;
        let crc = crc32c::crc32c(payload);
        frame[..4].copy_from_slice(&payload_len.to_le_bytes());
        frame[4..FRAME_HEADER_LEN].copy_from_slice(&crc.to_le_bytes());
        file.write_all(&frame)?;

        // 3. The restatement durable, then its name durable.
        file.sync_data()?;
        if let Some(dir) = path.parent() {
            File::open(dir)?.sync_all()?;
        }

        // 4. Switch. The old file handle closes when it drops; the file
        //    itself stays where it is, an archive. The sync thread holds a
        //    descriptor for the file it was started on, so it is replaced
        //    here too — otherwise every later barrier would faithfully sync
        //    the archive and say nothing about the segment being written.
        self.sync = SyncThread::spawn(&file).ok();
        self.file = file;
        self.file_bytes = HEADER_LEN + frame.len() as u64;
        self.next_seq = 2;
        Ok(true)
    }
}

impl Drop for WalWriter {
    fn drop(&mut self) {
        let _ = self.push_to_os();
    }
}

impl fmt::Debug for WalWriter {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("WalWriter")
            .field("next_seq", &self.next_seq)
            .field("buffered_bytes", &self.buf.len())
            .finish()
    }
}

// ------------------------------------------------------------- one writer

/// A held claim on one log file: while this value is alive, no other process
/// can open the same log for writing.
///
/// The log is written on the promise of a single appender — sequence numbers
/// are frame positions, and a torn tail is truncated on open. Two engines
/// sharing one file break both: they hand out the same sequence twice, and
/// the second one's open truncates whatever the first has not yet fsynced.
/// Nothing in the frame format can detect that afterwards, so it is stopped
/// at the door instead.
///
/// This is deliberately not the account lease in `engine-venue`. That one
/// guards a venue account against every process in the fleet and carries the
/// Python protocol's identity re-proof and note; this guards one file against
/// a second engine, needs no protocol, and must never write into the file it
/// is locking.
#[derive(Debug)]
pub struct WalLock {
    fd: libc::c_int,
    path: PathBuf,
}

impl WalLock {
    /// The log file being held.
    pub fn path(&self) -> &Path {
        &self.path
    }
}

impl Drop for WalLock {
    /// Closing releases the lock: `flock` lives on the open file description,
    /// and this is its only one. A crash releases it the same way, which is
    /// why there is no timeout to get wrong.
    fn drop(&mut self) {
        unsafe { libc::close(self.fd) };
    }
}

/// Why a log could not be claimed.
#[derive(Debug)]
pub enum WalLockError {
    /// Another process is writing this log right now.
    AlreadyHeld {
        path: PathBuf,
    },
    Io {
        path: PathBuf,
        source: io::Error,
    },
}

impl fmt::Display for WalLockError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            WalLockError::AlreadyHeld { path } => write!(
                f,
                "another engine is already writing the log at {}; two writers on one log \
                 corrupt it",
                path.display()
            ),
            WalLockError::Io { path, source } => {
                write!(f, "cannot claim the log at {}: {source}", path.display())
            }
        }
    }
}

impl std::error::Error for WalLockError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            WalLockError::Io { source, .. } => Some(source),
            WalLockError::AlreadyHeld { .. } => None,
        }
    }
}

/// Claim a log file for this process, without waiting and without touching a
/// byte of it.
///
/// Take this *before* opening the writer: [`WalWriter::open`] truncates a torn
/// tail, and doing that to a log another engine is appending to is the very
/// damage the claim is for. Symlinks are followed here, unlike the account
/// lease — the log path comes from the operator's own config, where pointing
/// it at another disk is a reasonable thing to do.
pub fn lock(path: impl AsRef<Path>) -> Result<WalLock, WalLockError> {
    let path = path.as_ref();
    let failed = |source: io::Error| WalLockError::Io {
        path: path.to_path_buf(),
        source,
    };

    let c_path = CString::new(path.as_os_str().as_bytes()).map_err(|_| {
        failed(io::Error::new(
            io::ErrorKind::InvalidInput,
            "path has a zero byte",
        ))
    })?;
    // O_CREAT so the claim can be made before the log exists; no truncation,
    // and nothing is ever written through this descriptor.
    let flags = libc::O_RDWR | libc::O_CREAT | libc::O_CLOEXEC;
    const OWNER_ONLY: libc::c_uint = 0o600;
    let fd = unsafe { libc::open(c_path.as_ptr(), flags, OWNER_ONLY) };
    if fd < 0 {
        return Err(failed(io::Error::last_os_error()));
    }
    let held = WalLock {
        fd,
        path: path.to_path_buf(),
    };

    if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } < 0 {
        let err = io::Error::last_os_error();
        let busy = matches!(
            err.raw_os_error(),
            Some(code) if code == libc::EWOULDBLOCK || code == libc::EAGAIN
        );
        return if busy {
            Err(WalLockError::AlreadyHeld {
                path: held.path.clone(),
            })
        } else {
            Err(failed(err))
        };
    }
    Ok(held)
}

/// Read a log without changing it: the good prefix, stopping exactly where a
/// writer would truncate. A bad header is still an error.
pub fn replay(path: impl AsRef<Path>) -> Result<Vec<(u64, WalRecord)>, WalError> {
    Ok(replay_scan(path)?.0)
}

/// Like [`replay`], and also says whether bytes exist past the good prefix —
/// a torn or corrupt tail that a writer would truncate. The audit command
/// prints that fact rather than silently hiding a crash point.
pub fn replay_scan(path: impl AsRef<Path>) -> Result<(Vec<(u64, WalRecord)>, bool), WalError> {
    let mut file = File::open(path.as_ref())?;
    let len = file.metadata()?.len();
    if len == 0 {
        return Ok((Vec::new(), false));
    }
    let scan = scan_file(&mut file, len)?;
    let damaged_tail = scan.good_end < len;
    Ok((scan.records, damaged_tail))
}

// ------------------------------------------------------------- segments

/// The name of segment `index` in `family`'s directory. Segment 1 is the
/// family path itself — every log written before rotation existed is
/// segment 1 of its own family, unchanged.
fn segment_path(family: &Path, index: u64) -> PathBuf {
    if index <= 1 {
        return family.to_path_buf();
    }
    let mut name = family.as_os_str().to_os_string();
    name.push(format!(".{index:06}"));
    PathBuf::from(name)
}

/// Every segment of this family that exists, ascending by number. The family
/// file itself is index 1 when present.
pub fn segments(family: &Path) -> Result<Vec<(u64, PathBuf)>, WalError> {
    let mut found: Vec<(u64, PathBuf)> = Vec::new();
    if family.exists() {
        found.push((1, family.to_path_buf()));
    }
    let dir = family.parent().filter(|p| !p.as_os_str().is_empty());
    let dir = dir
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    let Some(stem) = family.file_name().map(|n| n.to_string_lossy().into_owned()) else {
        return Ok(found);
    };
    let prefix = format!("{stem}.");
    for entry in std::fs::read_dir(&dir)? {
        let entry = entry?;
        let name = entry.file_name().to_string_lossy().into_owned();
        let Some(suffix) = name.strip_prefix(&prefix) else {
            continue;
        };
        if suffix.len() == 6 && suffix.bytes().all(|b| b.is_ascii_digit()) {
            if let Ok(index) = suffix.parse::<u64>() {
                if index >= 2 {
                    found.push((index, entry.path()));
                }
            }
        }
    }
    found.sort_by_key(|(index, _)| *index);
    Ok(found)
}

/// Whether boot may replay this segment alone.
///
/// Segment 1 always: it is the whole history up to the first rotation, so
/// there is nothing before it to restate. A later segment only if its first
/// record reads back as a complete [`WalRecord::SegmentBase`] — the frame
/// checksum makes a torn restatement mechanically detectable, and a torn
/// restatement means the rotation never finished, so the segment before it
/// is still the truth.
fn trusted(index: u64, records: &[(u64, WalRecord)]) -> bool {
    if index <= 1 {
        return true;
    }
    matches!(records.first(), Some((_, WalRecord::SegmentBase { .. })))
}

/// Read one candidate segment for the trust decision. A numbered segment
/// whose own header never finished — the crash landed inside the first eight
/// bytes of a rotation — reads as empty, exactly like a torn restatement,
/// rather than as an error. The family file gets no such pass, and
/// corruption past a good header is refused for both: real records that
/// cannot be read are not ours to skip.
fn scan_candidate(index: u64, path: &Path) -> Result<(Vec<(u64, WalRecord)>, bool), WalError> {
    match replay_scan(path) {
        Ok(read) => Ok(read),
        Err(WalError::Corrupt { offset: 0, .. }) if index > 1 => Ok((Vec::new(), false)),
        Err(e) => Err(e),
    }
}

/// Open the newest segment boot can trust, replaying it. This is how the
/// engine opens its log: a family that was never rotated is just its own
/// single file, so every log written before segments existed opens the same
/// way it always did.
///
/// Take [`lock`] on the FAMILY path first, exactly as before — one flock
/// covers every segment and the rotation between them, so there is no window
/// where a second engine could claim the directory.
pub fn open_current(
    family: impl AsRef<Path>,
) -> Result<(WalWriter, Vec<(u64, WalRecord)>), WalError> {
    let family = family.as_ref();
    let mut chain = segments(family)?;
    while let Some((index, path)) = chain.pop() {
        // Read-only first: a candidate that fails the trust check must not
        // be truncated or written to — it is either an archive or the
        // leftover of a rotation that never finished, and both stay as
        // evidence.
        let (records, _) = scan_candidate(index, &path)?;
        if trusted(index, &records) {
            return WalWriter::open_segment(&path, family);
        }
    }
    // Nothing exists yet: a fresh log at the family path.
    WalWriter::open_segment(family, family)
}

/// Replay a whole family in order, for the offline readers: every good record
/// of every segment, renumbered consecutively. The flag says whether the
/// NEWEST trusted segment ends in a torn tail — the crash point a writer
/// would truncate. Torn leftovers of abandoned rotations (a numbered segment
/// with no complete restatement) hold no records of their own and are
/// skipped without raising it.
///
/// Restatement records are left in the stream. They are written as "set
/// state to this", and at their position in the chain that state is exactly
/// what the records before them already produced, so readers that apply them
/// see the same history either way.
pub fn replay_chain(family: impl AsRef<Path>) -> Result<(Vec<(u64, WalRecord)>, bool), WalError> {
    let family = family.as_ref();
    let chain = segments(family)?;
    let newest_trusted = chain
        .iter()
        .rev()
        .find_map(|(index, path)| {
            let read = scan_candidate(*index, path);
            match read {
                Ok((records, damaged)) if trusted(*index, &records) => Some(Ok((*index, damaged))),
                Ok(_) => None,
                Err(e) => Some(Err(e)),
            }
        })
        .transpose()?;
    let mut out: Vec<(u64, WalRecord)> = Vec::new();
    let mut damaged = false;
    for (index, path) in &chain {
        let (records, torn) = scan_candidate(*index, path)?;
        if !trusted(*index, &records) {
            // An abandoned rotation: its only content was a restatement that
            // never finished. Boot never replayed it and neither do we.
            continue;
        }
        if let Some((newest, newest_damaged)) = newest_trusted {
            if *index == newest {
                damaged = newest_damaged;
            } else if torn {
                // An archive should never be torn — rotation syncs it before
                // the next segment exists — so damage here is worth a flag.
                damaged = true;
            }
        }
        for (_, record) in records {
            let seq = out.len() as u64 + 1;
            out.push((seq, record));
        }
    }
    Ok((out, damaged))
}

struct Scan {
    records: Vec<(u64, WalRecord)>,
    /// Offset just past the last good frame.
    good_end: u64,
}

fn scan_file(file: &mut File, len: u64) -> Result<Scan, WalError> {
    file.seek(SeekFrom::Start(0))?;
    let mut reader = BufReader::new(file);

    if len < HEADER_LEN {
        return Err(WalError::Corrupt {
            offset: 0,
            detail: "file is too short to carry the log header".to_string(),
        });
    }
    let mut header = [0u8; HEADER_LEN as usize];
    reader.read_exact(&mut header)?;
    if header != MAGIC {
        return Err(WalError::Corrupt {
            offset: 0,
            detail: "not an engine log: the magic at the start does not match".to_string(),
        });
    }

    let mut records: Vec<(u64, WalRecord)> = Vec::new();
    let mut payload: Vec<u8> = Vec::new();
    let mut offset = HEADER_LEN;

    loop {
        if len - offset < FRAME_HEADER_LEN as u64 {
            break;
        }
        let mut frame_header = [0u8; FRAME_HEADER_LEN];
        reader.read_exact(&mut frame_header)?;
        let payload_len = u32::from_le_bytes([
            frame_header[0],
            frame_header[1],
            frame_header[2],
            frame_header[3],
        ]) as u64;
        let want_crc = u32::from_le_bytes([
            frame_header[4],
            frame_header[5],
            frame_header[6],
            frame_header[7],
        ]);

        // We never write an empty record, and a torn write usually leaves a run
        // of zero bytes, whose checksum would otherwise look valid.
        if payload_len == 0 {
            break;
        }
        // A frame claiming more bytes than the file holds is a torn tail. This
        // also catches stray magic mid-file: "EWAL" reads as an impossible
        // length. The check keeps the read below bounded by the file size.
        if len - offset - (FRAME_HEADER_LEN as u64) < payload_len {
            break;
        }

        payload.resize(payload_len as usize, 0);
        reader.read_exact(&mut payload)?;
        if crc32c::crc32c(&payload) != want_crc {
            return Err(WalError::Corrupt {
                offset,
                detail: "frame checksum does not match".to_string(),
            });
        }

        // Checksum good but the bytes are not a record we understand: the disk
        // is fine and the data is real, so refuse rather than delete it.
        let record: WalRecord =
            serde_json::from_slice(&payload).map_err(|e| WalError::Corrupt {
                offset,
                detail: format!("frame passed its checksum but is not a readable record: {e}"),
            })?;

        offset += FRAME_HEADER_LEN as u64 + payload_len;
        let seq = records.len() as u64 + 1;
        records.push((seq, record));
    }

    Ok(Scan {
        records,
        good_end: offset,
    })
}

/// What one buffered append and one durability barrier cost, in microseconds.
#[derive(Clone, Copy, Debug, Default)]
pub struct Costs {
    pub appends: usize,
    pub append_p50_us: f64,
    pub append_p99_us: f64,
    pub append_max_us: f64,
    pub barriers: usize,
    pub barrier_p50_us: f64,
    pub barrier_p99_us: f64,
    pub barrier_max_us: f64,
}

impl fmt::Display for Costs {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(
            f,
            "append  n={:<7} p50={:>8.3} us  p99={:>8.3} us  max={:>8.3} us\n\
             barrier n={:<7} p50={:>8.3} us  p99={:>8.3} us  max={:>8.3} us",
            self.appends,
            self.append_p50_us,
            self.append_p99_us,
            self.append_max_us,
            self.barriers,
            self.barrier_p50_us,
            self.barrier_p99_us,
            self.barrier_max_us,
        )
    }
}

/// Time `appends` buffered appends and `barriers` durability barriers against
/// a real file. Each barrier is preceded by an append, so it measures the cost
/// the engine actually pays before sending an order.
pub fn measure(path: &Path, appends: usize, barriers: usize) -> Result<Costs, WalError> {
    let (mut wal, _) = WalWriter::open(path)?;
    let record = sample_record();

    for _ in 0..64 {
        wal.append(&record)?;
    }
    wal.flush()?;

    let mut append_us = Vec::with_capacity(appends);
    for _ in 0..appends {
        let t = Instant::now();
        wal.append(&record)?;
        append_us.push(t.elapsed().as_nanos() as f64 / 1000.0);
    }
    wal.flush()?;

    let mut barrier_us = Vec::with_capacity(barriers);
    for _ in 0..barriers {
        wal.append(&record)?;
        let t = Instant::now();
        wal.barrier()?;
        barrier_us.push(t.elapsed().as_nanos() as f64 / 1000.0);
    }

    append_us.sort_by(f64::total_cmp);
    barrier_us.sort_by(f64::total_cmp);
    Ok(Costs {
        appends,
        append_p50_us: quantile(&append_us, 0.50),
        append_p99_us: quantile(&append_us, 0.99),
        append_max_us: append_us.last().copied().unwrap_or(0.0),
        barriers,
        barrier_p50_us: quantile(&barrier_us, 0.50),
        barrier_p99_us: quantile(&barrier_us, 0.99),
        barrier_max_us: barrier_us.last().copied().unwrap_or(0.0),
    })
}

fn quantile(sorted: &[f64], q: f64) -> f64 {
    if sorted.is_empty() {
        return 0.0;
    }
    let idx = ((sorted.len() - 1) as f64 * q).round() as usize;
    sorted[idx]
}

/// The record the hot path writes before a send, used as the measurement's
/// representative size.
fn sample_record() -> WalRecord {
    use engine_types::ids::{StrategyId, SymbolId};
    use engine_types::orders::{OrderKind, OrderRequest, Side, StopSpec, TimeInForce};

    WalRecord::OrderSent {
        request: OrderRequest {
            client_order_id: "eng-0000000000000042".to_string(),
            strategy: StrategyId(1),
            symbol: SymbolId(7),
            side: Side::Buy,
            qty: 0.015,
            kind: OrderKind::Limit {
                px: 64123.5,
                tif: TimeInForce::PostOnly,
            },
            stop: Some(StopSpec {
                trigger_px: 63500.0,
            }),
            reduce_only: false,
        },
        wire_ns: 1_234_567_890,
        // Carried on the real record too, so the size this measures is the
        // size the hot path actually writes.
        arrival_mid: 64_120.25,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The invariant that has no other observable: the thread that runs a
    /// barrier must hold a descriptor for the file being written. Rotation is
    /// the only thing that breaks it, and it breaks it silently — the bytes
    /// stay readable from the page cache whichever file was synced.
    #[test]
    fn the_durability_thread_follows_the_log_across_a_rotation() {
        let dir = std::env::temp_dir().join(format!("wal-sync-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("rotate.wal");
        let _ = std::fs::remove_file(&path);
        for stale in segments(&path).unwrap_or_default() {
            let _ = std::fs::remove_file(stale.1);
        }

        let (mut wal, _) = WalWriter::open(&path).unwrap();
        let (synced, written) = wal.sync_and_write_inodes();
        assert_eq!(synced, Some(written), "a fresh log syncs what it writes");

        let base = WalRecord::Note {
            source: "test".to_string(),
            text: "segment base stand-in".to_string(),
        };
        assert!(wal.rotate(&base).unwrap());

        let (synced, written) = wal.sync_and_write_inodes();
        assert_eq!(
            synced,
            Some(written),
            "the barrier still syncs the archive, so it proves nothing about the live segment"
        );
        drop(wal);
        std::fs::remove_dir_all(&dir).ok();
    }
}
