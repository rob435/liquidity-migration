use std::io::{self, Read, Write};

use engine_types::strategy_process::MAX_PROCESS_FRAME_BYTES;
use serde::{de::DeserializeOwned, Serialize};

pub struct Budget {
    remaining: usize,
}

impl Budget {
    pub fn new(bytes: usize) -> Self {
        Self { remaining: bytes }
    }

    fn take(&mut self, bytes: usize) -> io::Result<()> {
        self.remaining = self.remaining.checked_sub(bytes).ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::InvalidData,
                "strategy callback exceeds its byte budget",
            )
        })?;
        Ok(())
    }
}

struct RecordWriter<'a, W> {
    destination: &'a mut W,
    budget: &'a mut Budget,
    frame: Vec<u8>,
}

impl<W: Write> RecordWriter<'_, W> {
    fn frame(&mut self) -> io::Result<()> {
        if self.frame.is_empty() {
            return Ok(());
        }
        self.budget.take(self.frame.len() + 4)?;
        self.destination
            .write_all(&(self.frame.len() as u32).to_le_bytes())?;
        self.destination.write_all(&self.frame)?;
        self.frame.clear();
        Ok(())
    }
}

impl<W: Write> Write for RecordWriter<'_, W> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        let mut remaining = bytes;
        while !remaining.is_empty() {
            let count = remaining
                .len()
                .min(MAX_PROCESS_FRAME_BYTES - self.frame.len());
            self.frame.extend_from_slice(&remaining[..count]);
            remaining = &remaining[count..];
            if self.frame.len() == MAX_PROCESS_FRAME_BYTES {
                self.frame()?;
            }
        }
        Ok(bytes.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.frame()?;
        self.destination.flush()
    }
}

pub fn write_record<W: Write, T: Serialize>(
    destination: &mut W,
    value: &T,
    budget: &mut Budget,
) -> io::Result<()> {
    let mut writer = RecordWriter {
        destination,
        budget,
        frame: Vec::with_capacity(MAX_PROCESS_FRAME_BYTES),
    };
    serde_json::to_writer(&mut writer, value).map_err(io::Error::other)?;
    writer.frame()?;
    writer.budget.take(4)?;
    writer.destination.write_all(&0u32.to_le_bytes())?;
    writer.destination.flush()
}

pub fn read_record<R: Read, T: DeserializeOwned>(
    source: &mut R,
    budget: &mut Budget,
) -> io::Result<T> {
    let mut payload = Vec::new();
    loop {
        let mut header = [0u8; 4];
        source.read_exact(&mut header)?;
        let count = u32::from_le_bytes(header) as usize;
        if count > MAX_PROCESS_FRAME_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "strategy frame exceeds its byte limit",
            ));
        }
        budget.take(count + 4)?;
        if count == 0 {
            break;
        }
        let start = payload.len();
        payload.resize(start + count, 0);
        source.read_exact(&mut payload[start..])?;
    }
    serde_json::from_slice(&payload).map_err(io::Error::other)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_large_checkpoint_crosses_frames_without_changing_bytes() {
        let value = vec![7_u8; MAX_PROCESS_FRAME_BYTES * 3];
        let mut encoded = Vec::new();
        write_record(&mut encoded, &value, &mut Budget::new(2_000_000)).unwrap();
        let decoded: Vec<u8> =
            read_record(&mut encoded.as_slice(), &mut Budget::new(2_000_000)).unwrap();
        assert_eq!(decoded, value);
    }

    #[test]
    fn a_declared_oversized_frame_is_refused_before_reading_its_body() {
        let header = ((MAX_PROCESS_FRAME_BYTES + 1) as u32).to_le_bytes();
        let error = read_record::<_, Vec<u8>>(&mut header.as_slice(), &mut Budget::new(usize::MAX))
            .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("frame exceeds"));
    }

    #[test]
    fn one_callback_budget_covers_every_record_not_each_record_independently() {
        let mut encoded = Vec::new();
        let mut budget = Budget::new(20);
        write_record(&mut encoded, &"12345678", &mut budget).unwrap();
        let error = write_record(&mut encoded, &"12345678", &mut budget).unwrap_err();
        assert!(error.to_string().contains("byte budget"));
    }
}
