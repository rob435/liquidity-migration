//! One writer per venue account, shared with the Python fleet.
//!
//! The Python side already has a single-writer rule, in
//! `liquidity_migration/account/account_owner_lease.py`. This is the engine
//! joining it, not a second rule standing beside it: the directory, the file
//! name, the open flags, the lock, the identity re-proof after the lock, and
//! the note written into the file are all copied from there. A lease that
//! differed in any one of those would protect nothing — the two processes
//! would hold two different locks and each would believe it was alone.
//!
//! The lock is a kernel `flock` on a file named after the venue's own account
//! id, so every process that could send an order to that account contends for
//! the same file whatever config, path, or credential started it.
//!
//! There is no heartbeat and no expiry. The kernel drops the lock when the
//! holder's last descriptor closes, which happens on a clean exit and on a
//! crash alike, and that is the only expiry that cannot be wrong. A timeout
//! would have to guess whether a slow holder was dead, and guessing wrong
//! means two writers.
//!
//! `mainnet` appears here as a realm — half of a lock file's name. It is not
//! an endpoint: this crate still knows only the two demo hosts, and the fence
//! in `tests/venue_fence.rs` still reads the whole crate back to prove it. A
//! lease names an account; it does not reach one.

use std::ffi::CString;
use std::fmt;
use std::io;
use std::mem::MaybeUninit;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::DirBuilderExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

/// Where lease files live. Python's units are handed this directory by
/// systemd's `RuntimeDirectory`; it is written out here rather than derived
/// because the two systems must name the same string or they are not sharing
/// a lock.
pub const LEASE_DIRECTORY: &str = "/run/lock/liquidity-migration";

/// Bybit's practice account.
pub const REALM_DEMO: &str = "demo";
/// The funded account. Naming it is not reaching it — see the module note.
pub const REALM_MAINNET: &str = "mainnet";

/// Owner read and write, the mode Python's own lease creates the file with.
/// Only used when the file does not exist yet; an existing file keeps the
/// permissions whoever made it chose.
const LEASE_FILE_MODE: libc::c_uint = 0o600;

/// The lock directory's mode, again matching Python's `mkdir(mode=0o700)`.
const LEASE_DIRECTORY_MODE: u32 = 0o700;

/// Enough of the holder's note to identify them. The note is one short line;
/// a file longer than this is not one of ours and truncating the read is the
/// right answer either way.
const NOTE_READ_LIMIT: usize = 4096;

/// A held lease: while this value is alive, this process is the one writer
/// for that venue account.
#[derive(Debug)]
pub struct AccountLease {
    /// Dropping this closes the descriptor, and closing the last descriptor
    /// on an open file description is what releases the lock. That is the
    /// whole release path — a clean stop and a crash take the same one.
    _fd: Fd,
    path: PathBuf,
}

impl AccountLease {
    /// The lock file being held.
    pub fn path(&self) -> &Path {
        &self.path
    }
}

/// Who holds a lease, for a caller that wants to know without taking it.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LeaseHolder {
    /// Nobody. The file may still exist carrying a dead holder's note, which
    /// is exactly why this answer is not read from the note.
    Free,
    /// Someone, right now. `note` is what they wrote into the file.
    Held { note: Option<String> },
}

/// Why a lease could not be taken.
#[derive(Debug)]
pub enum LeaseError {
    /// Another open file description holds it. `holder` is what they wrote
    /// into the file, so an operator is told who to go and look at rather
    /// than just "busy".
    AlreadyHeld {
        path: PathBuf,
        holder: Option<String>,
    },
    /// The file that got locked is not the file at the path any more, so the
    /// lock guards an inode nobody else will ever open.
    Replaced { path: PathBuf },
    /// A realm that is not `demo` or `mainnet`. Defaulting would put two
    /// accounts on one lock or one account on two.
    UnknownRealm { given: String },
    /// A user id that is not a whole number above zero.
    UnknownUserId { given: String },
    /// A lease with no role says nothing to whoever finds it stuck.
    EmptyRole,
    Io {
        path: PathBuf,
        doing: &'static str,
        source: io::Error,
    },
}

impl fmt::Display for LeaseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            LeaseError::AlreadyHeld { path, holder } => {
                write!(f, "another process already holds the account lease at {}", path.display())?;
                match holder {
                    Some(note) => write!(f, ", and left this note: {note}"),
                    None => write!(f, ", and left no note saying who it is"),
                }
            }
            LeaseError::Replaced { path } => write!(
                f,
                "the lease file at {} was replaced while it was being locked, \
                 so the lock would have guarded a file nobody else can see",
                path.display()
            ),
            LeaseError::UnknownRealm { given } => write!(
                f,
                "an account lease is for {REALM_DEMO} or {REALM_MAINNET}, not {given:?}"
            ),
            LeaseError::UnknownUserId { given } => write!(
                f,
                "an account lease is named by the venue's own account id, \
                 a whole number above zero, not {given:?}"
            ),
            LeaseError::EmptyRole => {
                write!(f, "an account lease has to say what is holding it")
            }
            LeaseError::Io { path, doing, source } => {
                write!(f, "cannot {doing} the account lease at {}: {source}", path.display())
            }
        }
    }
}

impl std::error::Error for LeaseError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            LeaseError::Io { source, .. } => Some(source),
            _ => None,
        }
    }
}

/// The lock file one account's lease lives at.
///
/// The inputs are normalized here as well as in [`acquire`], so a caller that
/// only wants to *name* the path — an operator message, a test — gets the
/// same string the lock itself uses.
pub fn canonical_path(realm: &str, user_id: &str) -> PathBuf {
    let realm = realm_text(realm).unwrap_or(realm);
    let user_id = account_id_text(user_id).unwrap_or_else(|| user_id.to_string());
    Path::new(LEASE_DIRECTORY).join(format!("bybit-{realm}-user-{user_id}.lock"))
}

/// Become the one writer for this venue account, or fail saying who already
/// is. The lease is held until the returned value is dropped or the process
/// dies.
pub fn acquire(realm: &str, user_id: &str, role: &str) -> Result<AccountLease, LeaseError> {
    let Some(realm) = realm_text(realm) else {
        return Err(LeaseError::UnknownRealm { given: realm.to_string() });
    };
    let Some(id) = account_id_text(user_id) else {
        return Err(LeaseError::UnknownUserId { given: user_id.to_string() });
    };
    acquire_at_for(&canonical_path(realm, &id), realm, role, Some(id.as_str()))
}

/// Take the lease at a path of the caller's choosing. Tests only — the live
/// path is [`acquire`], which is this with the canonical path filled in.
pub fn acquire_at(path: &Path, realm: &str, role: &str) -> Result<AccountLease, LeaseError> {
    acquire_at_for(path, realm, role, None)
}

fn acquire_at_for(
    path: &Path,
    realm: &str,
    role: &str,
    user_id: Option<&str>,
) -> Result<AccountLease, LeaseError> {
    acquire_at_with(path, realm, role, user_id, || {})
}

/// Whether anyone holds this account's lease right now, without taking it.
/// For a shadow run, which must never lock the live writer out.
pub fn probe(realm: &str, user_id: &str) -> Result<LeaseHolder, LeaseError> {
    let Some(realm) = realm_text(realm) else {
        return Err(LeaseError::UnknownRealm { given: realm.to_string() });
    };
    let Some(id) = account_id_text(user_id) else {
        return Err(LeaseError::UnknownUserId { given: user_id.to_string() });
    };
    probe_at(&canonical_path(realm, &id))
}

/// Whether anyone holds the lease at this path.
///
/// There is no way to ask `flock` a question without answering it: the only
/// test for "is it free" is to try to take it. So this does hold the lock for
/// the few microseconds between being granted it and giving it back, and a
/// process starting inside that window is told the lease is held. That
/// process then refuses to start — it does not start unprotected — and the
/// Python protocol probes exactly the same way, in
/// `revalidate_inherited_account_owner_lease`. This is the fleet's existing
/// behaviour rather than a new hazard.
pub fn probe_at(path: &Path) -> Result<LeaseHolder, LeaseError> {
    // No O_CREAT: a file that is not there is a lease nobody has ever taken,
    // and looking must not leave one behind. The mode below goes unused for
    // the same reason — nothing is being created.
    let flags = libc::O_RDWR | libc::O_NOFOLLOW | libc::O_CLOEXEC;
    let fd = match open_at_with_mode(path, flags, LEASE_FILE_MODE) {
        Ok(fd) => fd,
        Err(source) if source.kind() == io::ErrorKind::NotFound => return Ok(LeaseHolder::Free),
        Err(source) => {
            return Err(LeaseError::Io { path: path.to_path_buf(), doing: "look at", source })
        }
    };

    match flock_exclusive(fd.0) {
        Ok(()) => {
            unlock(fd.0);
            Ok(LeaseHolder::Free)
        }
        Err(source) if is_would_block(&source) => {
            Ok(LeaseHolder::Held { note: read_note(fd.0) })
        }
        Err(source) => {
            Err(LeaseError::Io { path: path.to_path_buf(), doing: "look at", source })
        }
    }
}

/// The whole of taking a lease.
///
/// `between` runs in the window between the open and the lock — the window
/// the identity re-proof below exists to close. A test drives it to prove
/// that proof is not decoration; production passes an empty closure and the
/// window is as short as two syscalls.
fn acquire_at_with(
    path: &Path,
    realm: &str,
    role: &str,
    user_id: Option<&str>,
    between: impl FnOnce(),
) -> Result<AccountLease, LeaseError> {
    let Some(realm) = realm_text(realm) else {
        return Err(LeaseError::UnknownRealm { given: realm.to_string() });
    };
    let role = role.trim();
    if role.is_empty() {
        return Err(LeaseError::EmptyRole);
    }

    if let Some(dir) = path.parent() {
        // Python's units are handed this directory by systemd. The engine can
        // be started by hand on a box where nothing has made it yet.
        std::fs::DirBuilder::new()
            .recursive(true)
            .mode(LEASE_DIRECTORY_MODE)
            .create(dir)
            .or_else(|e| if dir.is_dir() { Ok(()) } else { Err(e) })
            .map_err(|source| LeaseError::Io {
                path: dir.to_path_buf(),
                doing: "make the directory for",
                source,
            })?;
    }

    // O_NOFOLLOW so a symlink dropped at the path cannot redirect the lock
    // somewhere else; O_CLOEXEC so a child process cannot inherit the lease
    // and keep the account held after this process is gone; no truncation,
    // because the file already carries the note of whoever is in there.
    let flags = libc::O_RDWR | libc::O_CREAT | libc::O_NOFOLLOW | libc::O_CLOEXEC;
    let fd = open_at_with_mode(path, flags, LEASE_FILE_MODE).map_err(|source| LeaseError::Io {
        path: path.to_path_buf(),
        doing: "open",
        source,
    })?;
    // From here the descriptor is owned, so every early return below closes
    // it — and closing is also how a lock already taken gets given back.
    let lease = AccountLease { _fd: fd, path: path.to_path_buf() };
    let fd = lease._fd.0;

    between();

    match flock_exclusive(fd) {
        Ok(()) => {}
        Err(source) if is_would_block(&source) => {
            // Being refused is the normal answer, not a fault: somebody else
            // has the account. Read their note before letting go of the file.
            return Err(LeaseError::AlreadyHeld {
                path: lease.path.clone(),
                holder: read_note(fd),
            });
        }
        Err(source) => {
            return Err(LeaseError::Io { path: lease.path.clone(), doing: "lock", source })
        }
    }

    // After the lock, never before. A file unlinked and remade between the
    // open above and this lock would pass a check made before it, and would
    // still leave this lock sitting on an inode that has no name — the next
    // process opens the new file, is granted its own lock, and two writers
    // are live with nothing anywhere reporting an error.
    if !descriptor_is_the_file_at(fd, path) {
        return Err(LeaseError::Replaced { path: lease.path.clone() });
    }

    write_note(fd, &note(realm, role, user_id)).map_err(|source| LeaseError::Io {
        path: lease.path.clone(),
        doing: "write the note into",
        source,
    })?;
    Ok(lease)
}

/// The note written into the file, in Python's own encoding: JSON with the
/// keys sorted and a newline after it, so the file reads the same whichever
/// system took the lease and either can parse the other's.
///
/// Nothing reads this note to decide anything — the flock is the exclusion,
/// and Python re-proves its own hold from its own state. It is here so that
/// `cat` on a stuck lease names the holder.
///
/// The live Python holders write `{api_key_sha256, environment, pid, user_id,
/// venue}`. This writes the same minus `api_key_sha256`, plus `role` and
/// `started_at_ns`. The fingerprint is left out on purpose: it would mean
/// handing the credential to the lease module for a field no reader uses,
/// and the file name plus `user_id` already say which account this is.
/// `role` is the addition worth having — Python's note cannot tell you which
/// of its own processes is holding, and this one can.
fn note(realm: &str, role: &str, user_id: Option<&str>) -> String {
    let mut fields: Vec<(&str, String)> = vec![
        ("environment", quoted(realm)),
        ("pid", std::process::id().to_string()),
        ("role", quoted(role)),
        ("started_at_ns", wall_ns().to_string()),
    ];
    if let Some(user_id) = user_id {
        fields.push(("user_id", quoted(user_id)));
    }
    // Python writes `venue` for demo and leaves it out otherwise. Copied
    // rather than reasoned about: matching the file it writes is the point.
    if realm == REALM_DEMO {
        fields.push(("venue", quoted("bybit")));
    }
    fields.sort_by_key(|(key, _)| *key);
    let body: Vec<String> = fields
        .iter()
        .map(|(key, value)| format!("{}: {value}", quoted(key)))
        .collect();
    format!("{{{}}}\n", body.join(", "))
}

/// One JSON string. Every value in the note is a string or a number, so this
/// and `to_string` on the numbers are the whole encoder.
fn quoted(text: &str) -> String {
    serde_json::to_string(text).expect("a string is always encodable as JSON")
}

fn wall_ns() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|since| since.as_nanos())
        .unwrap_or(0)
}

/// `demo` or `mainnet`, however it was spelled and spaced. Anything else is
/// `None` — there is no default realm, because a wrong one is either two
/// accounts sharing a lock or one account holding two.
fn realm_text(raw: &str) -> Option<&'static str> {
    let trimmed = raw.trim();
    if trimmed.eq_ignore_ascii_case(REALM_DEMO) {
        Some(REALM_DEMO)
    } else if trimmed.eq_ignore_ascii_case(REALM_MAINNET) {
        Some(REALM_MAINNET)
    } else {
        None
    }
}

/// A venue account id as the lease path spells it: decimal digits, no leading
/// zeros, always above zero. `None` for anything else.
///
/// Python reaches the same string by `str(int(user_id))`, and its own check on
/// the canonical path is `user_id.isdigit() and int(user_id) > 0`. Working on
/// the digits rather than parsing into a fixed-width integer means an id
/// longer than 64 bits is normalized rather than refused.
pub fn account_id_text(raw: &str) -> Option<String> {
    let digits = raw.trim().strip_prefix('+').unwrap_or(raw.trim());
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let without_leading_zeros = digits.trim_start_matches('0');
    // Everything was zeros, so the number is zero: not an account.
    (!without_leading_zeros.is_empty()).then(|| without_leading_zeros.to_string())
}

// ------------------------------------------------------------ the syscalls

/// A descriptor that closes itself. Closing the last descriptor on an open
/// file description is what releases an `flock`, so this is the release path
/// as much as it is the cleanup.
#[derive(Debug)]
struct Fd(libc::c_int);

impl Drop for Fd {
    fn drop(&mut self) {
        // Nothing useful to do with a failure here: the descriptor is gone
        // either way, and so is the lock.
        unsafe { libc::close(self.0) };
    }
}

/// `mode` applies only when the flags create the file; the kernel ignores it
/// otherwise.
fn open_at_with_mode(path: &Path, flags: libc::c_int, mode: libc::c_uint) -> io::Result<Fd> {
    let c_path = CString::new(path.as_os_str().as_bytes())
        .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "the path contains a zero byte"))?;
    let fd = retry(|| unsafe { libc::open(c_path.as_ptr(), flags, mode) })?;
    Ok(Fd(fd))
}

/// Take the exclusive lock without waiting. Being refused is a distinct
/// answer, not a fault, and the caller separates the two with
/// [`is_would_block`].
fn flock_exclusive(fd: libc::c_int) -> io::Result<()> {
    retry(|| unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) })?;
    Ok(())
}

fn unlock(fd: libc::c_int) {
    unsafe { libc::flock(fd, libc::LOCK_UN) };
}

/// "Somebody else has it." The two spellings are the same number on Linux and
/// on macOS, but both are checked because the C standard does not promise it.
fn is_would_block(err: &io::Error) -> bool {
    matches!(err.raw_os_error(), Some(code) if code == libc::EWOULDBLOCK || code == libc::EAGAIN)
}

/// The locked descriptor is still the one and only file at `path`.
///
/// `flock` guards an inode, not a name, so this is what makes the lock mean
/// what the path says. Same device and inode: what was locked is what a new
/// contender would open. Both ordinary files: a directory or a device at that
/// path is not a lease. Exactly one name each: a second hard link is a second
/// name that could be renamed over this one, which would put the lock back on
/// an inode nobody opens.
fn descriptor_is_the_file_at(fd: libc::c_int, path: &Path) -> bool {
    let (Some(opened), Some(current)) = (fstat(fd), lstat(path)) else {
        return false;
    };
    is_regular(&opened)
        && is_regular(&current)
        && opened.st_nlink == 1
        && current.st_nlink == 1
        && opened.st_dev == current.st_dev
        && opened.st_ino == current.st_ino
}

fn fstat(fd: libc::c_int) -> Option<libc::stat> {
    let mut out = MaybeUninit::<libc::stat>::uninit();
    let ok = unsafe { libc::fstat(fd, out.as_mut_ptr()) } == 0;
    ok.then(|| unsafe { out.assume_init() })
}

/// Deliberately not following symlinks: the question is what is *at* the
/// path, not what it points to.
fn lstat(path: &Path) -> Option<libc::stat> {
    let c_path = CString::new(path.as_os_str().as_bytes()).ok()?;
    let mut out = MaybeUninit::<libc::stat>::uninit();
    let ok = unsafe { libc::lstat(c_path.as_ptr(), out.as_mut_ptr()) } == 0;
    ok.then(|| unsafe { out.assume_init() })
}

fn is_regular(entry: &libc::stat) -> bool {
    u32::from(entry.st_mode) & u32::from(libc::S_IFMT) == u32::from(libc::S_IFREG)
}

/// Replace the file's contents with the note and put it on the disk.
fn write_note(fd: libc::c_int, note: &str) -> io::Result<()> {
    // Truncate first: a note shorter than the last holder's would otherwise
    // leave their tail behind and the file would no longer parse as JSON.
    retry(|| unsafe { libc::ftruncate(fd, 0) })?;
    if unsafe { libc::lseek(fd, 0, libc::SEEK_SET) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let mut rest = note.as_bytes();
    while !rest.is_empty() {
        let written = retry_bytes(|| unsafe {
            libc::write(fd, rest.as_ptr().cast::<libc::c_void>(), rest.len())
        })?;
        if written == 0 {
            return Err(io::Error::new(io::ErrorKind::WriteZero, "the note made no progress"));
        }
        rest = &rest[written as usize..];
    }
    // The note is only worth anything to whoever comes looking after a crash,
    // so it has to reach the disk before this process starts trading.
    retry(|| unsafe { libc::fsync(fd) })?;
    Ok(())
}

/// Whatever the holder wrote into the file. Best effort on purpose: this is
/// the message an operator reads, and the lock does not depend on it.
fn read_note(fd: libc::c_int) -> Option<String> {
    if unsafe { libc::lseek(fd, 0, libc::SEEK_SET) } < 0 {
        return None;
    }
    let mut buf = [0u8; NOTE_READ_LIMIT];
    let read = retry_bytes(|| unsafe {
        libc::read(fd, buf.as_mut_ptr().cast::<libc::c_void>(), buf.len())
    })
    .ok()?;
    let text = String::from_utf8_lossy(&buf[..read as usize]).trim().to_string();
    (!text.is_empty()).then_some(text)
}

/// Run a syscall that answers -1 on failure, repeating it when a signal
/// interrupted it: an interrupted call did nothing, so doing it again is the
/// whole of the recovery.
fn retry(mut call: impl FnMut() -> libc::c_int) -> io::Result<libc::c_int> {
    loop {
        let answer = call();
        if answer >= 0 {
            return Ok(answer);
        }
        let err = io::Error::last_os_error();
        if err.raw_os_error() != Some(libc::EINTR) {
            return Err(err);
        }
    }
}

/// [`retry`] for the calls that count bytes rather than answer a flag.
fn retry_bytes(mut call: impl FnMut() -> libc::ssize_t) -> io::Result<libc::ssize_t> {
    loop {
        let answer = call();
        if answer >= 0 {
            return Ok(answer);
        }
        let err = io::Error::last_os_error();
        if err.raw_os_error() != Some(libc::EINTR) {
            return Err(err);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static COUNTER: AtomicU64 = AtomicU64::new(0);

    /// A throwaway lock directory, cleaned up on the way out. Tests must
    /// never touch /run/lock: the fleet's live lease lives there.
    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> TempDir {
            let mut path = std::env::temp_dir();
            let n = COUNTER.fetch_add(1, Ordering::Relaxed);
            path.push(format!("engine-lease-{}-{tag}-{n}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).unwrap();
            TempDir(path)
        }

        fn join(&self, name: &str) -> PathBuf {
            self.0.join(name)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn the_canonical_path_is_the_string_python_builds() {
        // Hardcoded, both sides. If either system changes how it spells this,
        // the two stop sharing a lock and nothing else would say so.
        assert_eq!(
            canonical_path("demo", "6039967"),
            PathBuf::from("/run/lock/liquidity-migration/bybit-demo-user-6039967.lock")
        );
        assert_eq!(
            canonical_path("mainnet", "112233"),
            PathBuf::from("/run/lock/liquidity-migration/bybit-mainnet-user-112233.lock")
        );
    }

    #[test]
    fn the_canonical_path_normalizes_the_way_python_does() {
        // Python reaches the file name through `str(int(user_id))`, so a
        // padded or spaced id lands on the same file rather than a second one.
        assert_eq!(
            canonical_path(" DEMO ", " 0006039967 "),
            canonical_path("demo", "6039967")
        );
    }

    #[test]
    fn a_second_holder_is_refused() {
        let dir = TempDir::new("second-holder");
        let path = dir.join("bybit-demo-user-1.lock");

        let first = acquire_at(&path, "demo", "engine").unwrap();
        assert_eq!(first.path(), path);

        // flock lives on the open file description, not on the process, so a
        // second open in this same process contends exactly as another
        // process would.
        let second = acquire_at(&path, "demo", "engine");
        let Err(LeaseError::AlreadyHeld { path: refused, holder }) = second else {
            panic!("a second holder was let in: {second:?}");
        };
        assert_eq!(refused, path);
        let note = holder.expect("the first holder's note");
        assert!(note.contains("\"role\": \"engine\""), "{note}");
    }

    #[test]
    fn letting_go_hands_the_account_to_the_next_holder() {
        let dir = TempDir::new("released");
        let path = dir.join("bybit-demo-user-2.lock");

        let first = acquire_at(&path, "demo", "engine").unwrap();
        drop(first);

        let second = acquire_at(&path, "demo", "reset").expect("the lease was not handed back");
        let note = std::fs::read_to_string(&path).unwrap();
        assert!(note.contains("\"role\": \"reset\""), "{note}");
        drop(second);
    }

    #[test]
    fn a_file_replaced_while_it_is_being_locked_is_refused() {
        let dir = TempDir::new("replaced");
        let path = dir.join("bybit-demo-user-3.lock");
        let replaced = path.clone();

        // Exactly the race the identity re-proof is for: the file we opened
        // is unlinked and a different one takes its name before the lock
        // lands, so the lock ends up on an inode no other process will ever
        // open.
        let taken = acquire_at_with(&path, "demo", "engine", None, move || {
            std::fs::remove_file(&replaced).unwrap();
            std::fs::write(&replaced, b"someone else's file\n").unwrap();
        });

        let Err(LeaseError::Replaced { path: named }) = taken else {
            panic!("a lock on an orphaned file was accepted: {taken:?}");
        };
        assert_eq!(named, path);

        // And the lock really was let go: the new file is free to take.
        let next = acquire_at(&path, "demo", "engine");
        assert!(next.is_ok(), "the refused attempt left the file locked: {next:?}");
    }

    #[test]
    fn a_second_name_for_the_lease_file_is_refused() {
        let dir = TempDir::new("hard-link");
        let path = dir.join("bybit-demo-user-4.lock");
        let other_name = dir.join("also-the-same-file.lock");
        std::fs::write(&path, b"").unwrap();
        std::fs::hard_link(&path, &other_name).unwrap();

        // Two names means a rename could put a different file at this path
        // while the lock stays where it is, so the lease is not the mutex it
        // claims to be.
        let taken = acquire_at(&path, "demo", "engine");
        assert!(
            matches!(taken, Err(LeaseError::Replaced { .. })),
            "a lease file with a second name was accepted: {taken:?}"
        );
    }

    #[test]
    fn a_symlink_at_the_lease_path_is_refused() {
        let dir = TempDir::new("symlink");
        let real = dir.join("elsewhere.lock");
        let path = dir.join("bybit-demo-user-5.lock");
        std::fs::write(&real, b"").unwrap();
        std::os::unix::fs::symlink(&real, &path).unwrap();

        // O_NOFOLLOW: a symlink dropped at the path must not redirect the
        // lock to a file of someone else's choosing.
        let taken = acquire_at(&path, "demo", "engine");
        assert!(
            matches!(taken, Err(LeaseError::Io { .. })),
            "a symlinked lease path was followed: {taken:?}"
        );
    }

    #[test]
    fn a_user_id_that_is_not_a_positive_whole_number_is_refused() {
        // Through the production entry point, and all before it touches a
        // disk — none of these reach /run/lock.
        for bad in ["", "  ", "abc", "0", "0000", "-1", "-0", "12.0", "1e3", "1 2", "٣"] {
            let refused = acquire(REALM_DEMO, bad, "engine");
            assert!(
                matches!(refused, Err(LeaseError::UnknownUserId { .. })),
                "{bad:?} was accepted as an account id: {refused:?}"
            );
        }
        assert_eq!(account_id_text("6039967").as_deref(), Some("6039967"));
        assert_eq!(account_id_text(" 0042 ").as_deref(), Some("42"));
        assert_eq!(account_id_text("+7").as_deref(), Some("7"));
    }

    #[test]
    fn an_unknown_realm_is_refused() {
        for bad in ["", "testnet", "paper", "live", "demo-2", "main"] {
            let refused = acquire(bad, "6039967", "engine");
            assert!(
                matches!(refused, Err(LeaseError::UnknownRealm { .. })),
                "{bad:?} was accepted as a realm: {refused:?}"
            );
        }
        assert_eq!(realm_text("Demo"), Some(REALM_DEMO));
        assert_eq!(realm_text(" mainnet\n"), Some(REALM_MAINNET));
    }

    #[test]
    fn a_lease_without_a_role_is_refused() {
        let dir = TempDir::new("no-role");
        let refused = acquire_at(&dir.join("bybit-demo-user-6.lock"), "demo", "   ");
        assert!(matches!(refused, Err(LeaseError::EmptyRole)), "{refused:?}");
    }

    #[test]
    fn the_note_carries_the_keys_python_writes() {
        let dir = TempDir::new("note");
        let path = dir.join("bybit-demo-user-7.lock");
        let lease = acquire_at(&path, "demo", "engine").unwrap();

        let raw = std::fs::read_to_string(&path).unwrap();
        assert!(raw.ends_with('\n'), "python writes a newline after it: {raw:?}");

        let parsed: serde_json::Value = serde_json::from_str(&raw).expect("the note is JSON");
        let fields = parsed.as_object().expect("the note is an object");
        let mut keys: Vec<&str> = fields.keys().map(String::as_str).collect();
        keys.sort_unstable();
        assert_eq!(keys, ["environment", "pid", "role", "started_at_ns", "venue"]);
        assert_eq!(fields["environment"], "demo");
        assert_eq!(fields["role"], "engine");
        assert_eq!(fields["venue"], "bybit");
        assert_eq!(fields["pid"].as_u64(), Some(u64::from(std::process::id())));
        assert!(fields["started_at_ns"].as_u64().unwrap() > 1_600_000_000_000_000_000);

        // Python's `json.dumps(..., sort_keys=True)` spelling, byte for byte,
        // so an operator reading the file cannot tell which system wrote it.
        assert!(raw.starts_with("{\"environment\": \"demo\", \"pid\": "), "{raw}");
        drop(lease);
    }

    #[test]
    fn a_mainnet_note_leaves_the_venue_key_out() {
        // Python adds `venue` for demo only. Matching the file it writes is
        // the whole point of writing one.
        let dir = TempDir::new("mainnet-note");
        let path = dir.join("bybit-mainnet-user-8.lock");
        let lease = acquire_at(&path, REALM_MAINNET, "engine").unwrap();
        let parsed: serde_json::Value =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
        assert!(parsed.get("venue").is_none(), "{parsed}");
        assert_eq!(parsed["environment"], "mainnet");
        drop(lease);
    }

    #[test]
    fn the_note_replaces_the_last_holders_rather_than_overwriting_part_of_it() {
        let dir = TempDir::new("truncate");
        let path = dir.join("bybit-demo-user-9.lock");
        std::fs::write(&path, "x".repeat(4000)).unwrap();

        let lease = acquire_at(&path, "demo", "engine").unwrap();
        let raw = std::fs::read_to_string(&path).unwrap();
        assert!(!raw.contains('x'), "the last holder's note survived: {raw:?}");
        serde_json::from_str::<serde_json::Value>(&raw).expect("the note is JSON");
        drop(lease);
    }

    #[test]
    fn a_probe_reads_the_holder_without_taking_the_lease() {
        let dir = TempDir::new("probe");
        let path = dir.join("bybit-demo-user-10.lock");

        // Never taken: no file, and looking must not make one.
        assert_eq!(probe_at(&path).unwrap(), LeaseHolder::Free);
        assert!(!path.exists(), "the probe created a lock file");

        let lease = acquire_at(&path, "demo", "engine").unwrap();
        let LeaseHolder::Held { note } = probe_at(&path).unwrap() else {
            panic!("a held lease was reported free");
        };
        assert!(note.unwrap().contains("\"role\": \"engine\""));

        drop(lease);
        assert_eq!(probe_at(&path).unwrap(), LeaseHolder::Free);
        // And the probe gave back what it borrowed: the lease is takeable.
        assert!(acquire_at(&path, "demo", "engine").is_ok());
    }

    #[test]
    fn the_lock_directory_is_made_when_it_is_missing() {
        let dir = TempDir::new("mkdir");
        let nested = dir.join("run").join("lock").join("liquidity-migration");
        let path = nested.join("bybit-demo-user-11.lock");

        let lease = acquire_at(&path, "demo", "engine").expect("the directory was not made");
        assert!(nested.is_dir());
        drop(lease);
    }

    #[test]
    fn the_default_path_is_the_canonical_one() {
        // `acquire` has one job before it reaches the disk: put the lease at
        // the path Python uses. Read the source back and prove it hands
        // `canonical_path` to the worker, the same way `demo_fence.rs` proves
        // the venue hosts. A runtime check would have to write into
        // /run/lock, which is the fleet's live lease.
        let source = std::fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("src/lease.rs"),
        )
        .unwrap();
        let body = source
            .split("pub fn acquire(realm: &str, user_id: &str, role: &str)")
            .nth(1)
            .expect("acquire is not spelled the way this test looks for it");
        let body = &body[..body.find("\n}").expect("acquire has no end")];
        assert!(
            body.contains("&canonical_path(realm, &id)"),
            "acquire builds its path some other way: {body}"
        );
    }

    #[test]
    fn the_note_names_the_account_as_well_as_the_file() {
        // The live Python holders put `user_id` in the note. A lease found
        // stuck should say which account it is holding without the reader
        // having to parse the file name.
        let written = note(REALM_DEMO, "engine", Some("579580669"));
        assert!(written.contains("\"user_id\": \"579580669\""), "{written}");
        // Sorted keys, the way Python's json.dumps(sort_keys=True) writes it.
        let keys: Vec<&str> = written
            .trim_start_matches('{')
            .split(", \"")
            .map(|part| part.split('"').next().unwrap_or("").trim_matches('"'))
            .collect();
        let mut sorted = keys.clone();
        sorted.sort_unstable();
        assert_eq!(keys, sorted, "keys must read in order: {written}");
    }
}
