//! Watching for the research system's target book.
//!
//! The book is a file another process writes. Reading a file blocks, so it is
//! not read on the loop thread at all: a worker task polls the path and posts
//! finished books down a channel that the loop receives from in its
//! `select!`. Same reason the market feed is built this way — the loop drops
//! the futures of every branch that did not win, so anything half-done inside
//! one would start over from nothing, and a poll interval longer than the
//! flush tick could never finish at all.
//!
//! What is delivered and what is not is the whole point:
//!
//! - a book that parses, at a version this engine knows: delivered;
//! - no file, an unreadable file, a bad parse, or a version we do not know:
//!   nothing is delivered and no strategy is woken. That is *no decision*,
//!   and a follower goes on holding what it holds.
//!
//! An empty `targets` list is not one of those. It parses, so it is delivered,
//! and it says hold nothing — a decision, and acted on like any other.
//! Getting those two the wrong way round flattens a live book the moment
//! research stops writing, which is exactly when it should not.

use std::path::PathBuf;
use std::time::{Duration, SystemTime};

use engine_types::{BookTarget, StrategyId, TargetBook};
use serde::Deserialize;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

/// The only book shape this engine reads. The producer bumps its version
/// exactly when an old reader would misread the file, so a number we do not
/// know is a file we must not try.
pub const KNOWN_VERSION: u64 = 1;

/// How often the path is looked at, unless a caller says otherwise. A carry
/// book changes daily; two seconds is already far quicker than it needs.
pub const DEFAULT_POLL: Duration = Duration::from_secs(2);

/// Books are rare and small. A queue this shallow is enough, and a full one
/// simply makes the worker wait, which is back-pressure and not a lost book.
const QUEUE_DEPTH: usize = 4;

/// Why a file was not a book.
#[derive(Clone, Debug, PartialEq)]
pub enum BookError {
    /// A version this engine does not read. Refused rather than guessed at.
    UnknownVersion { version: u64 },
    /// Not readable as a book: bad JSON, a missing field, a number that is
    /// not one.
    Malformed(String),
}

impl std::fmt::Display for BookError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            BookError::UnknownVersion { version } => write!(
                f,
                "target book is version {version} and this engine reads version {KNOWN_VERSION}"
            ),
            BookError::Malformed(detail) => write!(f, "target book is unreadable: {detail}"),
        }
    }
}

/// The file's shape, version 1.
///
/// Unknown keys are allowed on purpose: the producer bumps the version only
/// when an old reader would *misread* the file, so a version-1 book may
/// legitimately grow a field this engine has no use for.
#[derive(Debug, Deserialize)]
struct RawBook {
    source: String,
    decision_ts_ms: i64,
    valid_until_ms: i64,
    targets: Vec<RawTarget>,
}

#[derive(Debug, Deserialize)]
struct RawTarget {
    symbol: String,
    notional_usdt: f64,
    stop_loss_fraction: f64,
    leverage: f64,
}

/// Read one file's bytes as a book.
pub fn parse_book(bytes: &[u8]) -> Result<TargetBook, BookError> {
    let value: serde_json::Value =
        serde_json::from_slice(bytes).map_err(|e| BookError::Malformed(e.to_string()))?;

    // The version is read on its own, before any of the rest is believed. A
    // later shape may put something different under these same field names,
    // and reading it as version 1 is how a book gets silently misread.
    let Some(version) = value.get("version").and_then(|v| v.as_u64()) else {
        return Err(BookError::Malformed(
            "no version field, or it is not a whole number".to_string(),
        ));
    };
    if version != KNOWN_VERSION {
        return Err(BookError::UnknownVersion { version });
    }

    let raw: RawBook =
        serde_json::from_value(value).map_err(|e| BookError::Malformed(e.to_string()))?;

    // Nothing checks the numbers for being real here, because nothing can
    // arrive unreal: JSON has no word for infinity, and a literal too big for
    // an f64 is refused by the parse above, out of range.
    Ok(TargetBook {
        source: raw.source,
        decision_ts_ms: raw.decision_ts_ms,
        valid_until_ms: raw.valid_until_ms,
        targets: raw
            .targets
            .into_iter()
            .map(|t| BookTarget {
                symbol: t.symbol,
                notional_usdt: t.notional_usdt,
                stop_loss_fraction: t.stop_loss_fraction,
                leverage: t.leverage,
            })
            .collect(),
    })
}

/// The running watcher: where its books land, and the handle that stops it.
pub struct TargetBookWatcher {
    books: mpsc::Receiver<TargetBook>,
    worker: JoinHandle<()>,
}

impl TargetBookWatcher {
    /// Start watching a path. The first look happens straight away, so a book
    /// already on disk at boot is delivered without waiting out an interval.
    pub fn start(path: PathBuf) -> Self {
        Self::with_poll(path, DEFAULT_POLL)
    }

    pub fn with_poll(path: PathBuf, poll: Duration) -> Self {
        let (books, inbox) = mpsc::channel(QUEUE_DEPTH);
        let worker = BookWorker {
            path,
            poll,
            books,
            last_stamp: None,
            last_complaint: None,
        };
        TargetBookWatcher {
            books: inbox,
            worker: tokio::spawn(worker.run()),
        }
    }

    /// The next book, or `None` once the worker has gone for good. One
    /// channel receive and nothing else: dropped part-way it loses nothing,
    /// which is what the loop's `select!` needs.
    pub async fn next_book(&mut self) -> Option<TargetBook> {
        self.books.recv().await
    }
}

/// Every target book this engine watches, each bound to the one strategy that
/// asked for it.
///
/// Routing is by configuration, not by anything inside the file. A book's
/// `source` field says which producer wrote it and is for the log; trusting it
/// to decide who acts on a book would mean a producer could rename itself and
/// silently take over another sleeve's positions. So the engine is told, once,
/// which path belongs to which strategy, and a book reaches that strategy and
/// nobody else.
///
/// This is what lets one engine own an account that runs two sleeves. Before
/// it, every book went to every strategy, so two followers would each try to
/// hold the other's book — and since the venue keeps one position per symbol,
/// they would have fought over the same positions with no way to tell whose
/// was whose.
pub struct TargetBooks {
    watchers: Vec<(StrategyId, TargetBookWatcher)>,
    /// Where the next poll starts, so a book that arrives often cannot starve
    /// one that arrives rarely. Books are seconds apart at their fastest, so
    /// this is tidiness rather than a measured problem.
    start: usize,
}

impl TargetBooks {
    pub fn new(watchers: Vec<(StrategyId, TargetBookWatcher)>) -> Self {
        TargetBooks { watchers, start: 0 }
    }

    /// No paths were configured, so no book will ever arrive. Distinct from an
    /// empty book: this is *no decision at all*, and every follower holds
    /// whatever it holds.
    pub fn is_empty(&self) -> bool {
        self.watchers.is_empty()
    }

    pub fn len(&self) -> usize {
        self.watchers.len()
    }

    /// The next book and whose it is, or `None` once every watcher has gone.
    ///
    /// Cancel-safe: each watcher's own receive is, and this only polls them.
    pub async fn next_book(&mut self) -> Option<(StrategyId, TargetBook)> {
        std::future::poll_fn(|cx| self.poll_next(cx)).await
    }

    fn poll_next(
        &mut self,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Option<(StrategyId, TargetBook)>> {
        use std::task::Poll;
        let count = self.watchers.len();
        if count == 0 {
            return Poll::Ready(None);
        }
        for step in 0..count {
            let index = (self.start + step) % count;
            let (strategy, watcher) = &mut self.watchers[index];
            let strategy = *strategy;
            match watcher.books.poll_recv(cx) {
                Poll::Ready(Some(book)) => {
                    self.start = (index + 1) % count;
                    return Poll::Ready(Some((strategy, book)));
                }
                Poll::Ready(None) => {
                    // One worker stopped. Only that strategy loses its books;
                    // the others carry on, so this drops the dead one and
                    // starts the sweep again rather than closing the lot.
                    tracing::error!(
                        strategy = strategy.0,
                        "a target book watcher stopped; that strategy will get no more books"
                    );
                    self.watchers.remove(index);
                    self.start = 0;
                    return self.poll_next(cx);
                }
                Poll::Pending => {}
            }
        }
        Poll::Pending
    }
}

impl Drop for TargetBookWatcher {
    /// Nobody else holds the worker. Stop it with the watcher, or a dead
    /// engine leaves a task reading books nothing is listening for.
    fn drop(&mut self) {
        self.worker.abort();
    }
}

struct BookWorker {
    path: PathBuf,
    poll: Duration,
    books: mpsc::Sender<TargetBook>,
    /// Modification time and length of the file the last delivered book came
    /// from. Length as well as time because a filesystem's modification stamp
    /// can be coarser than the gap between two writes.
    last_stamp: Option<(SystemTime, u64)>,
    /// The last thing that went wrong, so a book that is simply not there yet
    /// does not write the same line every couple of seconds forever.
    last_complaint: Option<String>,
}

impl BookWorker {
    async fn run(mut self) {
        loop {
            if let Some(book) = self.look() {
                // A closed channel means the engine is gone.
                if self.books.send(book).await.is_err() {
                    return;
                }
            }
            tokio::time::sleep(self.poll).await;
        }
    }

    /// One look at the path. `None` for every kind of nothing-to-say: no
    /// file, an unreadable one, an unchanged one, a bad parse, a version we
    /// do not know. Nothing here ever ends the task — research writes on its
    /// own clock, and a watcher that gave up on the first bad read would go
    /// quiet exactly when the next good book was about to land.
    fn look(&mut self) -> Option<TargetBook> {
        let metadata = match std::fs::metadata(&self.path) {
            Ok(metadata) => metadata,
            Err(e) => {
                self.complain(format!("no target book to read ({e})"));
                return None;
            }
        };
        let modified = match metadata.modified() {
            Ok(modified) => modified,
            Err(e) => {
                self.complain(format!("this filesystem reports no modification time ({e})"));
                return None;
            }
        };
        let stamp = (modified, metadata.len());
        if self.last_stamp == Some(stamp) {
            return None;
        }

        let bytes = match std::fs::read(&self.path) {
            Ok(bytes) => bytes,
            Err(e) => {
                self.complain(format!("cannot read the target book ({e})"));
                return None;
            }
        };
        match parse_book(&bytes) {
            Ok(book) => {
                self.last_stamp = Some(stamp);
                self.last_complaint = None;
                tracing::info!(
                    path = %self.path.display(),
                    source = %book.source,
                    targets = book.targets.len(),
                    decision_ts_ms = book.decision_ts_ms,
                    valid_until_ms = book.valid_until_ms,
                    "read a target book"
                );
                Some(book)
            }
            // The stamp is deliberately not remembered, so the very next poll
            // tries the same file again. The book is written by rename so a
            // half-written read should be rare, but "rare" is not "never".
            Err(e) => {
                self.complain(e.to_string());
                None
            }
        }
    }

    fn complain(&mut self, what: String) {
        if self.last_complaint.as_deref() == Some(what.as_str()) {
            return;
        }
        tracing::warn!(path = %self.path.display(), "{what}");
        self.last_complaint = Some(what);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testpath::{temp_path, TempPath};

    const GOOD: &str = r#"{
      "version": 1,
      "source": "carry",
      "decision_ts_ms": 1700000000000,
      "valid_until_ms": 1700086400000,
      "targets": [
        {"symbol": "KAITOUSDT", "notional_usdt": 120.0, "stop_loss_fraction": 0.35, "leverage": 2.0},
        {"symbol": "COTIUSDT", "notional_usdt": -80.0, "stop_loss_fraction": 0.3, "leverage": 2.0}
      ]
    }"#;

    const EMPTY: &str = r#"{
      "version": 1,
      "source": "carry",
      "decision_ts_ms": 1700000000000,
      "valid_until_ms": 1700086400000,
      "targets": []
    }"#;

    fn write(path: &TempPath, text: &str) {
        // The producer writes by rename; do the same, so the test exercises
        // the reader against the shape it will actually meet.
        let tmp = path.path().with_extension("tmp");
        std::fs::write(&tmp, text).expect("writes the temp file");
        std::fs::rename(&tmp, path.path()).expect("renames into place");
    }

    #[test]
    fn a_whole_book_reads_back_field_for_field() {
        let book = parse_book(GOOD.as_bytes()).expect("the book parses");
        assert_eq!(book.source, "carry");
        assert_eq!(book.decision_ts_ms, 1_700_000_000_000);
        assert_eq!(book.valid_until_ms, 1_700_086_400_000);
        assert_eq!(book.targets.len(), 2);
        assert_eq!(book.targets[0].symbol, "KAITOUSDT");
        assert_eq!(book.targets[0].notional_usdt, 120.0);
        assert_eq!(book.targets[0].stop_loss_fraction, 0.35);
        assert_eq!(book.targets[0].leverage, 2.0);
        assert_eq!(book.targets[1].notional_usdt, -80.0, "a short keeps its sign");
    }

    #[test]
    fn an_empty_book_parses_it_is_a_decision_not_an_absence() {
        let book = parse_book(EMPTY.as_bytes()).expect("an empty book is still a book");
        assert!(book.targets.is_empty());
    }

    #[test]
    fn a_version_we_do_not_know_is_refused_by_name() {
        let text = GOOD.replace("\"version\": 1", "\"version\": 2");
        let err = parse_book(text.as_bytes()).expect_err("version 2 is not ours to read");
        assert_eq!(err, BookError::UnknownVersion { version: 2 });
        let words = err.to_string();
        assert!(words.contains('2') && words.contains('1'), "{words}");
    }

    #[test]
    fn a_book_with_no_version_at_all_is_not_guessed_at() {
        let text = GOOD.replace("\"version\": 1,", "");
        assert!(matches!(
            parse_book(text.as_bytes()),
            Err(BookError::Malformed(_))
        ));
    }

    #[test]
    fn half_a_file_and_a_missing_field_are_both_refused() {
        assert!(matches!(
            parse_book(&GOOD.as_bytes()[..GOOD.len() / 2]),
            Err(BookError::Malformed(_))
        ));
        let text = GOOD.replace("\"stop_loss_fraction\": 0.35,", "");
        assert!(matches!(
            parse_book(text.as_bytes()),
            Err(BookError::Malformed(_))
        ));
    }

    #[test]
    fn a_number_too_big_to_be_real_never_becomes_a_size() {
        // The reason there is no finiteness check in the parser: a literal
        // outside f64's range is refused as it is read, so an infinity can
        // never be built in the first place.
        let text = GOOD.replace("120.0", "1e400");
        let err = parse_book(text.as_bytes()).expect_err("infinity is not a size");
        assert!(matches!(err, BookError::Malformed(_)), "{err}");
        assert!(err.to_string().contains("out of range"), "{err}");
    }

    #[test]
    fn a_key_the_engine_does_not_read_is_tolerated() {
        // Version 1 may grow a field: the producer bumps the version only
        // when an old reader would misread the file, not when it gains one.
        let text = GOOD.replace("\"source\": \"carry\",", "\"source\": \"carry\", \"note\": \"hi\",");
        assert!(parse_book(text.as_bytes()).is_ok());
    }

    /// Poll fast so the tests do not sit through the shipping interval.
    fn watch(path: &TempPath) -> TargetBookWatcher {
        TargetBookWatcher::with_poll(path.path().to_path_buf(), Duration::from_millis(10))
    }

    async fn next(watcher: &mut TargetBookWatcher) -> TargetBook {
        tokio::time::timeout(Duration::from_secs(5), watcher.next_book())
            .await
            .expect("a book before the deadline")
            .expect("the watcher is still running")
    }

    /// Nothing arrived within the window. Not proof of "never", but the
    /// window is hundreds of polls wide.
    async fn nothing_for(watcher: &mut TargetBookWatcher, window: Duration) {
        if let Ok(book) = tokio::time::timeout(window, watcher.next_book()).await {
            panic!("expected no book, got {book:?}");
        }
    }

    #[tokio::test]
    async fn a_missing_file_delivers_nothing_and_the_first_book_still_arrives() {
        let path = temp_path("book-missing");
        let mut watcher = watch(&path);
        nothing_for(&mut watcher, Duration::from_millis(200)).await;
        write(&path, GOOD);
        assert_eq!(next(&mut watcher).await.targets.len(), 2);
    }

    #[tokio::test]
    async fn a_parse_error_does_not_kill_the_watcher_and_the_next_good_book_lands() {
        let path = temp_path("book-torn");
        write(&path, "{ this is not json");
        let mut watcher = watch(&path);
        nothing_for(&mut watcher, Duration::from_millis(200)).await;
        write(&path, GOOD);
        let book = next(&mut watcher).await;
        assert_eq!(book.source, "carry");
        assert_eq!(book.targets.len(), 2);
    }

    #[tokio::test]
    async fn a_version_we_do_not_know_delivers_no_book_at_all() {
        let path = temp_path("book-version");
        write(&path, &GOOD.replace("\"version\": 1", "\"version\": 99"));
        let mut watcher = watch(&path);
        nothing_for(&mut watcher, Duration::from_millis(200)).await;
        // And the watcher is still alive to read the one that follows.
        write(&path, EMPTY);
        assert!(next(&mut watcher).await.targets.is_empty());
    }

    #[tokio::test]
    async fn an_empty_book_is_delivered_because_holding_nothing_is_a_decision() {
        let path = temp_path("book-empty");
        write(&path, EMPTY);
        let mut watcher = watch(&path);
        assert!(next(&mut watcher).await.targets.is_empty());
    }

    #[tokio::test]
    async fn an_unchanged_file_is_read_once_and_a_changed_one_again() {
        let path = temp_path("book-unchanged");
        write(&path, GOOD);
        let mut watcher = watch(&path);
        assert_eq!(next(&mut watcher).await.targets.len(), 2);
        nothing_for(&mut watcher, Duration::from_millis(200)).await;
        write(&path, EMPTY);
        assert!(next(&mut watcher).await.targets.is_empty());
    }
}
