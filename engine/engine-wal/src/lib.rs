//! The engine's append-only log.
//!
//! Layout: an 8-byte magic header, then frames. One frame is
//! `[u32 LE payload length][u32 LE crc32c of the payload][payload]`, and the
//! payload is one [`WalRecord`] as JSON. Sequence numbers are not stored on
//! disk: a record's sequence is its frame index, counting from 1.
//!
//! One writer. Appends are buffered; [`Wal::flush`] hands the bytes to the OS,
//! [`Wal::barrier`] also waits for the disk and is the durability point used
//! before an order leaves the socket.
//!
//! On open the log is replayed. A tail that did not survive a crash — a short
//! frame, a frame that runs past the end of the file, or one whose checksum
//! does not match — is cut off, and appending resumes on that boundary. A bad
//! header is refused instead: a file that is not ours is not ours to truncate.

use std::fmt;
use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::time::Instant;

pub use engine_types::wal::{Wal, WalError, WalRecord};

/// Magic at offset 0. The trailing digits are the format version.
const MAGIC: [u8; 8] = *b"EWAL0001";
const HEADER_LEN: u64 = 8;
const FRAME_HEADER_LEN: usize = 8;

/// Buffered bytes go to the OS once they pass this, so a quiet log never
/// grows an unbounded buffer.
const BUFFER_HIGH_WATER: usize = 64 * 1024;

/// The append-only log writer.
pub struct WalWriter {
    file: File,
    /// Frames waiting to go to the OS. Reused across appends: a record is
    /// serialized straight into it, so a warm writer allocates nothing.
    buf: Vec<u8>,
    next_seq: u64,
}

impl WalWriter {
    /// Open an existing log or create a new one, replay what is on disk, and
    /// position for appending after the last good frame. Returns the writer
    /// and every record replayed, each with its sequence.
    pub fn open(path: impl AsRef<Path>) -> Result<(Self, Vec<(u64, WalRecord)>), WalError> {
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.as_ref())?;
        let len = file.metadata()?.len();

        let scan = if len == 0 {
            file.write_all(&MAGIC)?;
            Scan { records: Vec::new(), good_end: HEADER_LEN }
        } else {
            scan_file(&mut file, len)?
        };

        if scan.good_end < len {
            file.set_len(scan.good_end)?;
        }
        file.seek(SeekFrom::Start(scan.good_end))?;

        let next_seq = scan.records.len() as u64 + 1;
        let writer =
            WalWriter { file, buf: Vec::with_capacity(BUFFER_HIGH_WATER), next_seq };
        Ok((writer, scan.records))
    }

    /// The sequence the next append will get.
    pub fn next_seq(&self) -> u64 {
        self.next_seq
    }

    fn push_to_os(&mut self) -> Result<(), WalError> {
        if self.buf.is_empty() {
            return Ok(());
        }
        self.file.write_all(&self.buf)?;
        self.buf.clear();
        Ok(())
    }
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

    fn flush(&mut self) -> Result<(), WalError> {
        self.push_to_os()
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
        let payload_len =
            u32::from_le_bytes([frame_header[0], frame_header[1], frame_header[2], frame_header[3]])
                as u64;
        let want_crc =
            u32::from_le_bytes([frame_header[4], frame_header[5], frame_header[6], frame_header[7]]);

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
            break;
        }

        // Checksum good but the bytes are not a record we understand: the disk
        // is fine and the data is real, so refuse rather than delete it.
        let record: WalRecord = serde_json::from_slice(&payload).map_err(|e| WalError::Corrupt {
            offset,
            detail: format!("frame passed its checksum but is not a readable record: {e}"),
        })?;

        offset += FRAME_HEADER_LEN as u64 + payload_len;
        let seq = records.len() as u64 + 1;
        records.push((seq, record));
    }

    Ok(Scan { records, good_end: offset })
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
            kind: OrderKind::Limit { px: 64123.5, tif: TimeInForce::PostOnly },
            stop: Some(StopSpec { trigger_px: 63500.0 }),
            reduce_only: false,
        },
        wire_ns: 1_234_567_890,
    }
}
