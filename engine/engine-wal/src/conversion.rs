//! Offline v5 restatement conversion into a separate, complete WAL family.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::fd::AsRawFd;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use engine_types::strategy_process::CallbackWalReader;
use engine_types::trade::OpenTradeLot;

use crate::{read_record, scan_frames, segment_path, segments, WalError, WalRecord, MAGIC};

#[derive(Debug, PartialEq, Eq)]
pub struct Conversion {
    pub family: PathBuf,
    pub segments: u64,
    pub records: u64,
    pub upgraded_bases: u64,
    pub relocated_bases: u64,
}

fn invalid(detail: impl Into<String>) -> WalError {
    WalError::Io(io::Error::new(io::ErrorKind::InvalidData, detail.into()))
}

/// Convert only v5 bases; retain all other wire kinds and every source frame.
/// `lots` must return the base's existing accounting replay, including unknown
/// cost basis. The input must be stopped; the output directory must not exist.
pub fn v5_to_v7(
    input: &Path,
    output_dir: &Path,
    mut lots: impl FnMut(&WalRecord) -> Result<Vec<OpenTradeLot>, String>,
) -> Result<Conversion, WalError> {
    let name = input
        .file_name()
        .ok_or_else(|| invalid("input WAL has no family filename"))?;
    if name
        .to_str()
        .and_then(|name| name.rsplit_once('.'))
        .is_some_and(|(_, suffix)| {
            suffix
                .parse::<u64>()
                .is_ok_and(|index| index >= 2 && suffix == format!("{index:06}"))
        })
    {
        return Err(invalid(
            "use the WAL family base path, not a numbered segment",
        ));
    }
    let claim = File::open(input)?;
    if unsafe { libc::flock(claim.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } < 0 {
        return Err(WalError::Io(io::Error::last_os_error()));
    }
    let chain = segments(input)?;
    for (at, (index, _)) in chain.iter().enumerate() {
        if *index != at as u64 + 1 {
            return Err(invalid(format!("missing WAL source segment {}", at + 1)));
        }
    }
    let mut callbacks = crate::callback_reader::Reader {
        file: claim.try_clone()?,
        segment: 1,
        family: input.to_path_buf(),
        cancel: None,
        conversion_v5: true,
    };
    fs::DirBuilder::new().mode(0o700).create(output_dir)?;
    let family = output_dir.join(name);
    let mut result = Conversion {
        family,
        segments: 0,
        records: 0,
        upgraded_bases: 0,
        relocated_bases: 0,
    };
    let converted = (|| {
        for (index, path) in chain {
            let mut source = File::open(&path)?;
            let len = source.metadata()?.len();
            let mut target = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(segment_path(&result.family, index))?;
            target.write_all(&MAGIC)?;
            let mut count = 0;
            let end = scan_frames(
                &mut source,
                len,
                read_source_record,
                |sequence, _, payload, record| {
                    if index > 1
                        && sequence == 1
                        && !matches!(record, WalRecord::SegmentBase { .. })
                    {
                        return Err(invalid(format!("segment {index} has no complete base")));
                    }
                    validate_cursors(&record, &mut callbacks)?;
                    let converted = convert_base(payload, record, &mut lots, &mut result)?;
                    let payload = converted.as_deref().unwrap_or(payload);
                    let length = u32::try_from(payload.len())
                        .map_err(|_| invalid("converted WAL frame exceeds u32 length"))?;
                    target.write_all(&length.to_le_bytes())?;
                    target.write_all(&crc32c::crc32c(payload).to_le_bytes())?;
                    target.write_all(payload)?;
                    count += 1;
                    Ok(())
                },
            )?;
            if end != len || (index > 1 && count == 0) {
                return Err(invalid(format!(
                    "segment {index} is incomplete; input is unchanged"
                )));
            }
            target.sync_all()?;
            result.segments += 1;
            result.records += count;
        }
        File::open(output_dir)?.sync_all()?;
        if let Some(parent) = crate::parent_dir(output_dir) {
            File::open(parent)?.sync_all()?;
        }
        Ok(())
    })();
    if let Err(error) = converted {
        fs::remove_dir_all(output_dir)?;
        return Err(error);
    }
    Ok(result)
}

fn read_source_record(payload: &[u8]) -> Result<WalRecord, serde_json::Error> {
    let mut value = crate::record_value::parse(payload)?;
    if value.get("kind").and_then(serde_json::Value::as_str) == Some("segment_base_v5") {
        crate::validate_base_fields(&value, "segment_base_v5")?;
        value["kind"] = "segment_base".into();
    }
    crate::read_record_value(value)
}

fn validate_cursors(
    record: &WalRecord,
    reader: &mut crate::callback_reader::Reader,
) -> Result<(), WalError> {
    let WalRecord::SegmentBase {
        strategy_callback_queues,
        strategy_callback_sources,
        ..
    } = record
    else {
        return Ok(());
    };
    for cursor in strategy_callback_queues
        .iter()
        .flat_map(|slot| std::iter::once(slot.queued).chain(slot.prepared))
        .chain(strategy_callback_sources.iter().map(|source| source.cursor))
    {
        if cursor.segment == 0 {
            return Err(invalid("callback source segment is zero"));
        }
        let located = reader.locate(engine_types::strategy_process::CallbackWalCursor {
            offset: 0,
            ..cursor
        })?;
        if cursor.offset != 0 && located.offset != cursor.offset {
            return Err(invalid(
                "callback byte offset disagrees with its segment and sequence",
            ));
        }
    }
    for slot in strategy_callback_queues {
        reader.read_callback(slot.queued, slot.callback_id)?;
        if let Some(prepared) = slot.prepared {
            reader.read_callback(prepared, slot.callback_id)?;
        }
    }
    Ok(())
}

fn convert_base(
    payload: &[u8],
    mut record: WalRecord,
    lots: &mut impl FnMut(&WalRecord) -> Result<Vec<OpenTradeLot>, String>,
    result: &mut Conversion,
) -> Result<Option<Vec<u8>>, WalError> {
    if !matches!(record, WalRecord::SegmentBase { .. }) {
        return Ok(None);
    }
    let mut value: serde_json::Value =
        serde_json::from_slice(payload).map_err(crate::json_error)?;
    let upgraded = value["kind"] == "segment_base_v5";
    let materialized = if upgraded {
        Some(lots(&record).map_err(invalid)?)
    } else {
        None
    };
    let WalRecord::SegmentBase {
        open_trade_lots,
        legacy_signal_source_retirements,
        strategy_callback_queues,
        strategy_callback_sources,
        ..
    } = &mut record
    else {
        unreachable!()
    };
    if let Some(materialized) = materialized {
        value["kind"] = "segment_base_v7".into();
        value["open_trade_lots"] =
            serde_json::to_value(&materialized).map_err(crate::json_error)?;
        value["legacy_signal_source_retirements"] =
            serde_json::to_value(legacy_signal_source_retirements).map_err(crate::json_error)?;
        *open_trade_lots = Some(materialized);
        result.upgraded_bases += 1;
    }
    let mut relocated = false;
    for cursor in strategy_callback_queues
        .iter_mut()
        .flat_map(|slot| std::iter::once(&mut slot.queued).chain(slot.prepared.as_mut()))
        .chain(
            strategy_callback_sources
                .iter_mut()
                .map(|source| &mut source.cursor),
        )
    {
        relocated |= cursor.offset != 0;
        cursor.offset = 0;
    }
    if relocated {
        value["strategy_callback_queues"] =
            serde_json::to_value(strategy_callback_queues).map_err(crate::json_error)?;
        value["strategy_callback_sources"] =
            serde_json::to_value(strategy_callback_sources).map_err(crate::json_error)?;
        result.relocated_bases += 1;
    }
    if !upgraded && !relocated {
        return Ok(None);
    }
    let encoded = serde_json::to_vec(&value).map_err(crate::json_error)?;
    let restored = read_record(&encoded).map_err(crate::json_error)?;
    if restored != record {
        return Err(invalid(
            "converted base does not preserve its decoded state",
        ));
    }
    Ok(Some(encoded))
}
