//! A minimal file log, used until `engine-wal` lands.
//!
//! Same shape as the contract in `docs/engine.md`: length-prefixed,
//! checksummed frames carrying a sequence number and a JSON body, appends
//! buffered, `barrier` forcing the bytes to disk before an order goes out,
//! `flush` the cheap group commit. Replay stops at the first frame that does
//! not check out, which is how a half-written tail from a crash is dropped.
//!
//! When `engine-wal` arrives, `assembly::wal` swaps to it and this file goes.
//! Anything reading a log written by the real crate must use the real crate's
//! reader — the two framings are not promised to match.

use std::fs::{File, OpenOptions};
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::sync::OnceLock;

use engine_types::{Wal, WalError, WalRecord};

const HEADER: usize = 16; // body length, sequence, checksum

pub struct FileWal {
    file: File,
    buf: Vec<u8>,
    seq: u64,
}

impl FileWal {
    /// Open for append, dropping a torn tail first.
    pub fn open(path: &Path) -> Result<Self, WalError> {
        if let Some(dir) = path.parent() {
            if !dir.as_os_str().is_empty() {
                std::fs::create_dir_all(dir)?;
            }
        }
        let scan = read_frames(path)?;
        // Never truncate: the log is the engine's memory, and opening it is
        // not a reason to forget.
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .read(true)
            .write(true)
            .open(path)?;
        let len = file.metadata()?.len();
        if scan.good_bytes < len {
            file.set_len(scan.good_bytes)?;
        }
        file.seek(SeekFrom::End(0))?;
        Ok(FileWal {
            file,
            buf: Vec::with_capacity(64 * 1024),
            seq: scan.last_seq,
        })
    }

    fn push_out(&mut self) -> Result<(), WalError> {
        if self.buf.is_empty() {
            return Ok(());
        }
        self.file.write_all(&self.buf)?;
        self.buf.clear();
        Ok(())
    }
}

impl Wal for FileWal {
    fn append(&mut self, record: &WalRecord) -> Result<u64, WalError> {
        self.seq += 1;
        let body = serde_json::to_vec(record)
            .map_err(|e| WalError::Io(std::io::Error::other(e.to_string())))?;
        let mut head = [0u8; HEADER];
        head[0..4].copy_from_slice(&(body.len() as u32).to_le_bytes());
        head[4..12].copy_from_slice(&self.seq.to_le_bytes());
        head[12..16].copy_from_slice(&checksum(self.seq, &body).to_le_bytes());
        self.buf.extend_from_slice(&head);
        self.buf.extend_from_slice(&body);
        Ok(self.seq)
    }

    fn barrier(&mut self) -> Result<(), WalError> {
        self.push_out()?;
        self.file.sync_data()?;
        Ok(())
    }

    fn flush(&mut self) -> Result<(), WalError> {
        self.push_out()?;
        self.file.flush()?;
        Ok(())
    }
}

pub struct Scan {
    pub records: Vec<WalRecord>,
    pub last_seq: u64,
    /// Bytes up to the end of the last frame that checked out.
    pub good_bytes: u64,
    /// True when bytes past `good_bytes` were found and ignored.
    pub torn_tail: bool,
}

/// Read every whole, intact record. A missing file reads as an empty log.
pub fn read_frames(path: &Path) -> Result<Scan, WalError> {
    let mut scan = Scan {
        records: Vec::new(),
        last_seq: 0,
        good_bytes: 0,
        torn_tail: false,
    };
    let mut file = match File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(scan),
        Err(e) => return Err(e.into()),
    };
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes)?;
    let mut at = 0usize;
    while at + HEADER <= bytes.len() {
        let len = u32::from_le_bytes(bytes[at..at + 4].try_into().unwrap()) as usize;
        let seq = u64::from_le_bytes(bytes[at + 4..at + 12].try_into().unwrap());
        let crc = u32::from_le_bytes(bytes[at + 12..at + 16].try_into().unwrap());
        let body_at = at + HEADER;
        if len > bytes.len().saturating_sub(body_at) {
            break;
        }
        let body = &bytes[body_at..body_at + len];
        if checksum(seq, body) != crc {
            break;
        }
        match serde_json::from_slice::<WalRecord>(body) {
            Ok(record) => scan.records.push(record),
            Err(_) => break,
        }
        scan.last_seq = seq;
        at = body_at + len;
        scan.good_bytes = at as u64;
    }
    scan.torn_tail = (scan.good_bytes as usize) < bytes.len();
    Ok(scan)
}

fn checksum(seq: u64, body: &[u8]) -> u32 {
    let table = crc_table();
    let mut crc = !0u32;
    for b in seq.to_le_bytes().iter().chain(body.iter()) {
        crc = (crc >> 8) ^ table[((crc ^ *b as u32) & 0xff) as usize];
    }
    !crc
}

fn crc_table() -> &'static [u32; 256] {
    static TABLE: OnceLock<[u32; 256]> = OnceLock::new();
    TABLE.get_or_init(|| {
        let mut table = [0u32; 256];
        for (i, slot) in table.iter_mut().enumerate() {
            let mut crc = i as u32;
            for _ in 0..8 {
                let mask = (crc & 1).wrapping_neg();
                crc = (crc >> 1) ^ (0x82F6_3B78 & mask);
            }
            *slot = crc;
        }
        table
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testpath::temp_path;

    fn note(text: &str) -> WalRecord {
        WalRecord::Note {
            source: "test".into(),
            text: text.into(),
        }
    }

    #[test]
    fn round_trips_records_and_continues_the_sequence() {
        let path = temp_path("filewal-round");
        {
            let mut wal = FileWal::open(&path).unwrap();
            assert_eq!(wal.append(&note("one")).unwrap(), 1);
            assert_eq!(wal.append(&note("two")).unwrap(), 2);
            wal.barrier().unwrap();
        }
        let scan = read_frames(&path).unwrap();
        assert_eq!(scan.records.len(), 2);
        assert_eq!(scan.last_seq, 2);
        assert!(!scan.torn_tail);

        let mut wal = FileWal::open(&path).unwrap();
        assert_eq!(wal.append(&note("three")).unwrap(), 3);
        wal.barrier().unwrap();
        assert_eq!(read_frames(&path).unwrap().records.len(), 3);
    }

    #[test]
    fn a_half_written_tail_is_dropped_and_overwritten() {
        let path = temp_path("filewal-torn");
        {
            let mut wal = FileWal::open(&path).unwrap();
            wal.append(&note("kept")).unwrap();
            wal.barrier().unwrap();
        }
        let whole = std::fs::metadata(&path).unwrap().len();
        {
            let mut f = OpenOptions::new().append(true).open(&path).unwrap();
            f.write_all(b"\x40\x00\x00\x00 half a frame").unwrap();
        }
        let scan = read_frames(&path).unwrap();
        assert_eq!(scan.records.len(), 1);
        assert!(scan.torn_tail);
        assert_eq!(scan.good_bytes, whole);

        let mut wal = FileWal::open(&path).unwrap();
        wal.append(&note("after")).unwrap();
        wal.barrier().unwrap();
        let scan = read_frames(&path).unwrap();
        assert_eq!(scan.records.len(), 2);
        assert!(!scan.torn_tail);
    }

    #[test]
    fn a_flipped_byte_stops_the_read_there() {
        let path = temp_path("filewal-bitflip");
        {
            let mut wal = FileWal::open(&path).unwrap();
            wal.append(&note("first")).unwrap();
            wal.append(&note("second")).unwrap();
            wal.barrier().unwrap();
        }
        let mut bytes = std::fs::read(&path).unwrap();
        let last = bytes.len() - 3;
        bytes[last] ^= 0xff;
        std::fs::write(&path, &bytes).unwrap();
        let scan = read_frames(&path).unwrap();
        assert_eq!(scan.records.len(), 1);
        assert!(scan.torn_tail);
    }
}
