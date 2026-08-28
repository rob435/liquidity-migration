//! Explicit operator reset for the durable daily-loss circuit breaker.

use std::error::Error;
use std::path::Path;

use engine_types::{Wal, WalRecord};

use crate::{assembly, config, flatness};

/// Inspect, and with `execute`, clear the risk control anchor.
///
/// The engine must be stopped (the WAL lock is held throughout), and the
/// credential-wide venue inventory must be flat. The reset is an auditable WAL
/// record, never an environment toggle or an in-memory flag.
pub async fn run(config_path: &Path, note: &str, execute: bool) -> Result<(), Box<dyn Error>> {
    let note = note.trim();
    if note.is_empty() {
        return Err("loss-reset requires a non-empty operator note".into());
    }
    if note.len() > 512 {
        return Err("loss-reset note is longer than 512 bytes".into());
    }

    let loaded = config::load(config_path)?;
    let wal_path = &loaded.config.engine.wal_path;
    let _claim = engine_wal::lock(wal_path)?;
    let (replayed, torn) = engine_wal::replay_chain(wal_path)?;
    let mut current: Option<String> = None;
    for (_, record) in &replayed {
        match record {
            WalRecord::ControlAnchor { source, state } if source == "risk" => {
                current = Some(state.clone());
            }
            WalRecord::SegmentBase {
                control_anchors, ..
            } => {
                current = control_anchors
                    .iter()
                    .find(|anchor| anchor.source == "risk")
                    .map(|anchor| anchor.state.clone());
            }
            _ => {}
        }
    }

    println!("log       {}", wal_path.display());
    println!(
        "risk      {}",
        if current.is_some() {
            "durable control anchor present"
        } else {
            "no durable control anchor"
        }
    );
    if torn {
        println!("log-tail   torn; execute would truncate only the incomplete record");
    }

    // This read is deliberately inside the log claim. The engine cannot race
    // the proof by opening another order between the flat sample and reset.
    flatness::run(config_path).await?;
    if !execute {
        println!("dry-run    no records written; repeat with --execute to clear the halt");
        return Ok(());
    }

    let (mut wal, _) = assembly::wal(wal_path)?;
    wal.append(&WalRecord::Note {
        source: "loss-reset".to_string(),
        text: note.to_string(),
    })?;
    wal.append(&WalRecord::ControlAnchor {
        source: "risk".to_string(),
        state: engine_risk::cleared_loss_guard_state(),
    })?;
    wal.barrier()?;
    println!("reset      durable; the next entry establishes a new UTC opening-equity anchor");
    Ok(())
}
