use std::fs::File;
use std::io::{self, BufReader, Read};
use std::os::unix::fs::FileExt;
use std::path::PathBuf;

use engine_types::strategy_process::{
    CallbackWalCursor, CallbackWalReader, CallbackWalRecord, StrategyCallbackInput,
};
use engine_types::{OrderUpdate, StrategyId};
use serde::de::{DeserializeSeed, IgnoredAny, MapAccess, SeqAccess, Visitor};

use crate::{WalError, FRAME_HEADER_LEN, HEADER_LEN};

pub(crate) struct Reader {
    pub file: File,
    pub segment: u64,
    pub family: PathBuf,
    pub cancel: Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
}

struct Window<'a> {
    file: &'a File,
    offset: u64,
    left: u64,
    crc: u32,
    budget: Option<std::sync::Arc<std::sync::atomic::AtomicU64>>,
    cancel: Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
}
impl Read for Window<'_> {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        if self
            .cancel
            .as_ref()
            .is_some_and(|cancel| cancel.load(std::sync::atomic::Ordering::Relaxed))
        {
            return Err(io::Error::other("order archive read cancelled"));
        }
        let mut count = bytes.len().min(self.left as usize);
        if let Some(budget) = &self.budget {
            let left = budget.load(std::sync::atomic::Ordering::Relaxed);
            if left == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidData,
                    "order lineage field exceeds byte limit",
                ));
            }
            count = count.min(left as usize);
        }
        if count == 0 {
            return Ok(0);
        }
        let read = self.file.read_at(&mut bytes[..count], self.offset)?;
        if read == 0 {
            return Err(io::Error::new(
                io::ErrorKind::UnexpectedEof,
                "callback WAL frame is incomplete",
            ));
        }
        self.crc = crc32c::crc32c_append(self.crc, &bytes[..read]);
        self.offset += read as u64;
        self.left -= read as u64;
        if let Some(budget) = &self.budget {
            budget.fetch_sub(read as u64, std::sync::atomic::Ordering::Relaxed);
        }
        Ok(read)
    }
}

#[derive(serde::Deserialize)]
struct Envelope {
    kind: String,
}

#[derive(serde::Deserialize)]
struct OrderEnvelope {
    callbacks: Option<Vec<StrategyId>>,
    update: Option<serde_json::Value>,
}

#[derive(serde::Deserialize)]
struct EventEnvelope {
    strategy: Option<StrategyId>,
    event: Option<engine_types::strategy_process::CallbackEvent>,
}

struct InputSeed(u64);
impl<'de> DeserializeSeed<'de> for InputSeed {
    type Value = Option<StrategyCallbackInput>;
    fn deserialize<D: serde::Deserializer<'de>>(
        self,
        deserializer: D,
    ) -> Result<Self::Value, D::Error> {
        struct InputVisitor(u64);
        impl<'de> Visitor<'de> for InputVisitor {
            type Value = Option<StrategyCallbackInput>;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("callback WAL frame")
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut found = None;
                while let Some(key) = map.next_key::<String>()? {
                    match key.as_str() {
                        "input" => {
                            let input: StrategyCallbackInput = map.next_value()?;
                            if input.callback_id == self.0 {
                                found = Some(input);
                            }
                        }
                        "strategy_callbacks" => found = map.next_value_seed(InputsSeed(self.0))?,
                        _ => {
                            map.next_value::<IgnoredAny>()?;
                        }
                    }
                }
                Ok(found)
            }
        }
        deserializer.deserialize_map(InputVisitor(self.0))
    }
}
struct InputsSeed(u64);
impl<'de> DeserializeSeed<'de> for InputsSeed {
    type Value = Option<StrategyCallbackInput>;
    fn deserialize<D: serde::Deserializer<'de>>(
        self,
        deserializer: D,
    ) -> Result<Self::Value, D::Error> {
        struct InputsVisitor(u64);
        impl<'de> Visitor<'de> for InputsVisitor {
            type Value = Option<StrategyCallbackInput>;
            fn expecting(&self, f: &mut std::fmt::Formatter) -> std::fmt::Result {
                f.write_str("legacy callback restatement")
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut found = None;
                while let Some(input) = seq.next_element::<StrategyCallbackInput>()? {
                    if input.callback_id == self.0 {
                        if found.is_some() {
                            return Err(serde::de::Error::custom(
                                "callback identity repeated in restatement",
                            ));
                        }
                        found = Some(input);
                    }
                }
                Ok(found)
            }
        }
        deserializer.deserialize_seq(InputsVisitor(self.0))
    }
}

impl Reader {
    pub(crate) fn select(&mut self, segment: u64) -> Result<(), WalError> {
        if segment != self.segment {
            let file = File::open(crate::segment_path(&self.family, segment))?;
            let mut header = [0; HEADER_LEN as usize];
            file.read_exact_at(&mut header, 0)?;
            if header != crate::MAGIC {
                return Err(WalError::Corrupt {
                    offset: 0,
                    detail: "callback segment has an invalid WAL header".into(),
                });
            }
            self.file = file;
            self.segment = segment;
        }
        Ok(())
    }

    fn locate(&mut self, mut cursor: CallbackWalCursor) -> Result<CallbackWalCursor, WalError> {
        self.select(cursor.segment)?;
        if cursor.sequence == 0 {
            return Err(WalError::Corrupt {
                offset: cursor.offset,
                detail: "callback sequence is zero".into(),
            });
        }
        if cursor.offset == 0 {
            cursor.offset = HEADER_LEN;
            for _ in 1..cursor.sequence {
                let (length, _) = self.header(cursor)?;
                cursor.offset += FRAME_HEADER_LEN as u64 + length;
            }
        }
        Ok(cursor)
    }

    pub(crate) fn header(&self, cursor: CallbackWalCursor) -> Result<(u64, u32), WalError> {
        let corrupt = |detail: &str| WalError::Corrupt {
            offset: cursor.offset,
            detail: detail.into(),
        };
        let length = self.file.metadata()?.len();
        if cursor.offset < HEADER_LEN
            || cursor.offset > length
            || length - cursor.offset < FRAME_HEADER_LEN as u64
        {
            return Err(corrupt("callback cursor is outside a complete WAL frame"));
        }
        let mut header = [0; FRAME_HEADER_LEN];
        self.file.read_exact_at(&mut header, cursor.offset)?;
        let payload_len =
            u32::from_le_bytes(header[..4].try_into().expect("four-byte length")) as u64;
        if payload_len == 0 || length - cursor.offset - (FRAME_HEADER_LEN as u64) < payload_len {
            return Err(corrupt("callback WAL source frame is incomplete"));
        }
        Ok((
            payload_len,
            u32::from_le_bytes(header[4..].try_into().expect("four-byte checksum")),
        ))
    }

    fn envelope<T: serde::de::DeserializeOwned>(
        &self,
        cursor: CallbackWalCursor,
        length: u64,
        crc: u32,
    ) -> Result<T, WalError> {
        let mut reader = self.frame(cursor, length);
        let envelope = serde_json::from_reader(&mut reader).map_err(crate::json_error)?;
        let consumed = reader.into_inner();
        if consumed.left != 0 || consumed.crc != crc {
            return Err(WalError::Corrupt {
                offset: cursor.offset,
                detail: "callback WAL source checksum does not match".into(),
            });
        }
        Ok(envelope)
    }

    fn frame(&self, cursor: CallbackWalCursor, length: u64) -> BufReader<Window<'_>> {
        self.bounded_frame(cursor, length, None)
    }

    fn bounded_frame(
        &self,
        cursor: CallbackWalCursor,
        length: u64,
        budget: Option<std::sync::Arc<std::sync::atomic::AtomicU64>>,
    ) -> BufReader<Window<'_>> {
        BufReader::with_capacity(
            64 * 1024,
            Window {
                file: &self.file,
                offset: cursor.offset + FRAME_HEADER_LEN as u64,
                left: length,
                crc: 0,
                budget,
                cancel: self.cancel.clone(),
            },
        )
    }
    pub(crate) fn decode<T, S>(
        &self,
        cursor: CallbackWalCursor,
        length: u64,
        crc: u32,
        budget: std::sync::Arc<std::sync::atomic::AtomicU64>,
        seed: S,
    ) -> Result<T, WalError>
    where
        S: for<'de> DeserializeSeed<'de, Value = T>,
    {
        let mut reader = self.bounded_frame(cursor, length, Some(budget));
        let mut json = serde_json::Deserializer::from_reader(&mut reader);
        let result = seed.deserialize(&mut json).map_err(crate::json_error)?;
        json.end().map_err(crate::json_error)?;
        let consumed = reader.into_inner();
        if consumed.left != 0 || consumed.crc != crc {
            return Err(WalError::Corrupt {
                offset: cursor.offset,
                detail: "order lineage WAL checksum does not match".into(),
            });
        }
        Ok(result)
    }
    pub(crate) fn record(
        &self,
        cursor: CallbackWalCursor,
        length: u64,
    ) -> Result<engine_types::WalRecord, WalError> {
        if length > crate::order_lineage::MAX_ROW_BYTES {
            return Err(WalError::Corrupt {
                offset: cursor.offset,
                detail: "order lineage record exceeds byte limit".into(),
            });
        }
        let mut bytes = Vec::new();
        self.frame(cursor, length).read_to_end(&mut bytes)?;
        crate::read_record(&bytes).map_err(crate::json_error)
    }
}

impl CallbackWalReader for Reader {
    fn start(&self) -> CallbackWalCursor {
        CallbackWalCursor {
            segment: self.segment,
            sequence: 1,
            offset: HEADER_LEN,
        }
    }

    fn read_callback(
        &mut self,
        cursor: CallbackWalCursor,
        callback_id: u64,
    ) -> Result<StrategyCallbackInput, WalError> {
        let cursor = self.locate(cursor)?;
        let (length, crc) = self.header(cursor)?;
        let mut reader = self.frame(cursor, length);
        let mut json = serde_json::Deserializer::from_reader(&mut reader);
        let input = InputSeed(callback_id)
            .deserialize(&mut json)
            .map_err(crate::json_error)?;
        json.end().map_err(crate::json_error)?;
        let consumed = reader.into_inner();
        if consumed.left != 0 || consumed.crc != crc {
            return Err(WalError::Corrupt {
                offset: cursor.offset,
                detail: "callback WAL source checksum does not match".into(),
            });
        }
        input.ok_or_else(|| WalError::Corrupt {
            offset: cursor.offset,
            detail: "callback slot has no input in its durable frame".into(),
        })
    }

    fn next(&mut self, cursor: CallbackWalCursor) -> Result<Option<CallbackWalRecord>, WalError> {
        let mut cursor = self.locate(cursor)?;
        if cursor.offset == self.file.metadata()?.len() {
            let next = cursor
                .segment
                .checked_add(1)
                .ok_or_else(|| WalError::Corrupt {
                    offset: cursor.offset,
                    detail: "callback segment exhausted".into(),
                })?;
            if !crate::segment_path(&self.family, next).exists() {
                return Ok(None);
            }
            self.select(next)?;
            cursor = self.start();
        }
        let (length, crc) = self.header(cursor)?;
        let envelope: Envelope = self.envelope(cursor, length, crc)?;
        let corrupt = |detail: &str| WalError::Corrupt {
            offset: cursor.offset,
            detail: detail.into(),
        };
        let source = if envelope.kind == "order_update_v2" {
            let envelope: OrderEnvelope = self.envelope(cursor, length, crc)?;
            let owners = envelope
                .callbacks
                .ok_or_else(|| corrupt("order callback source has no owner metadata"))?;
            let mut update = envelope
                .update
                .ok_or_else(|| corrupt("order callback source has no parent update"))?;
            if let Some(fill) = update
                .get_mut("Fill")
                .and_then(serde_json::Value::as_object_mut)
            {
                if fill.get("fee_known") == Some(&serde_json::Value::Bool(false)) {
                    fill.insert("fee".into(), serde_json::Value::Null);
                }
            }
            let update: OrderUpdate = serde_json::from_value(update).map_err(crate::json_error)?;
            Some((
                owners,
                engine_types::strategy_process::CallbackEvent::Order { update },
            ))
        } else if envelope.kind == "recovered_fill_v2" {
            if length > engine_types::strategy_process::MAX_PROCESS_PROPOSAL_BYTES as u64 {
                return Err(corrupt(
                    "recovered callback source exceeds the process input bound",
                ));
            }
            let mut bytes = Vec::new();
            self.frame(cursor, length).read_to_end(&mut bytes)?;
            let record = crate::read_record(&bytes).map_err(crate::json_error)?;
            let (owners, update) = record
                .recovered_callback()
                .ok_or_else(|| corrupt("recovered callback source has no owner metadata"))?;
            Some((
                owners,
                engine_types::strategy_process::CallbackEvent::Order { update },
            ))
        } else if envelope.kind == "strategy_callback_source" {
            let envelope: EventEnvelope = self.envelope(cursor, length, crc)?;
            Some((
                vec![envelope
                    .strategy
                    .ok_or_else(|| corrupt("callback source has no owner"))?],
                envelope
                    .event
                    .ok_or_else(|| corrupt("callback source has no event"))?,
            ))
        } else {
            None
        };
        Ok(Some(CallbackWalRecord {
            cursor,
            next: CallbackWalCursor {
                segment: cursor.segment,
                sequence: cursor
                    .sequence
                    .checked_add(1)
                    .ok_or_else(|| corrupt("callback source sequence exhausted"))?,
                offset: cursor.offset + FRAME_HEADER_LEN as u64 + length,
            },
            source,
        }))
    }
}

#[cfg(test)]
mod cancellation_tests {
    use super::*;
    use std::sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    };

    #[test]
    fn cancelling_between_frame_reads_stops_json_byte_iteration() {
        use std::io::Write;
        let mut file = tempfile::tempfile().unwrap();
        file.write_all(b"remaining frame payload").unwrap();
        let cancel = Arc::new(AtomicBool::new(false));
        let mut window = Window {
            file: &file,
            offset: 0,
            left: 23,
            crc: 0,
            budget: None,
            cancel: Some(cancel.clone()),
        };
        assert_eq!(window.read(&mut [0; 1]).unwrap(), 1);
        cancel.store(true, Ordering::Relaxed);
        let error = window.read(&mut [0; 1]).unwrap_err();
        assert_ne!(
            error.kind(),
            io::ErrorKind::Interrupted,
            "JSON byte iteration retries Interrupted forever"
        );
        assert!(std::io::BufReader::new(window)
            .bytes()
            .next()
            .unwrap()
            .unwrap_err()
            .to_string()
            .contains("cancelled"));
    }
}
