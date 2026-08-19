//! `engine reconcile-clear`: the deliberate look the may-open latch waits
//! for.
//!
//! When boot finds orders or exposure the log cannot account for, it latches
//! `may_open` false and every later boot says "it will reduce only until
//! somebody looks at the log". This command IS that look, made executable:
//! it holds the log's own lock (so no engine is running against it), shows
//! the standing findings and the latch, and — only with `--execute` —
//! appends one [`WalRecord::LatchCleared`]: the exposure ledger restated to
//! the venue's own positions, the absorbed findings kept as the receipt,
//! and the latch reset.
//!
//! It weakens nothing. The next boot runs the same comparison it always
//! ran; a difference that appears after the clear latches again. What this
//! removes is only the memory of debt an operator has already read —
//! history the venue no longer serves and no recovery can replay.

use std::error::Error;
use std::path::Path;

use engine_types::{Side, SymbolId, SymbolTotal, VenueGateway, Wal, WalRecord};

use crate::assembly;
use crate::clock;
use crate::config;
use crate::inflight::LedgerOfOrders;
use crate::reconcile;
use crate::replay::LogNames;

pub async fn run(config_path: &Path, note: &str, execute: bool) -> Result<(), Box<dyn Error>> {
    let loaded = config::load(config_path)?;
    let settings = &loaded.config.engine;

    // The same claim a running engine holds. Getting it proves nothing is
    // appending to this log; failing it means stop the engine first.
    let _log_claim = engine_wal::lock(&settings.wal_path)?;
    let (mut wal, replayed) = assembly::wal(&settings.wal_path)?;

    let names = LogNames::of_log(&replayed);
    let mut venue = assembly::venue(&settings.venue, names.symbols.clone())?;
    let who = venue.account_identity().await?;
    println!("log     {}", settings.wal_path.display());
    println!("account {} ({})", who.user_id, who.realm);

    let latched = replayed.iter().rev().find_map(|record| match record {
        WalRecord::Reconciled { may_open, .. } => Some(*may_open),
        WalRecord::SegmentBase { may_open, .. } => Some(*may_open),
        WalRecord::LatchCleared { .. } => Some(true),
        _ => None,
    });
    println!(
        "latch   may_open = {}",
        match latched {
            Some(open) => open.to_string(),
            None => "never judged".to_string(),
        }
    );

    let account = venue.account_view().await?;
    let working = venue.working_orders().await?;
    let mut steps: Vec<Option<f64>> = vec![None; names.symbols.len()];
    match venue.instrument_rules().await {
        Ok(rules) => {
            for (name, rule) in rules {
                if let Some(at) = names.symbols.iter().position(|n| *n == name) {
                    steps[at] = Some(rule.qty_step);
                }
            }
        }
        Err(e) => println!("no instrument rules ({e}); judging with the smallest tolerance"),
    }

    let orders = LedgerOfOrders::from_records(&replayed);
    let symbols = names.symbols.clone();
    let found = reconcile::reconcile(
        &orders,
        &replayed,
        &working,
        &account,
        |name| {
            symbols
                .iter()
                .position(|n| n == name)
                .map(|at| SymbolId(at as u16))
        },
        |id| steps.get(id.0 as usize).copied().flatten(),
    );

    if found.findings.is_empty() {
        println!("standing findings: none — the log and the venue agree");
    } else {
        println!("standing findings:");
        for line in found.lines() {
            println!("  {line}");
        }
    }

    // What the restatement will say: the venue's positions, signed, which is
    // the one truth about what is held.
    let restated: Vec<SymbolTotal> = account
        .positions
        .iter()
        .map(|position| SymbolTotal {
            symbol: position.symbol,
            signed_qty: match position.side {
                Side::Buy => position.qty,
                Side::Sell => -position.qty,
            },
        })
        .collect();
    println!("restating exposure over {} symbol(s):", restated.len());
    for row in &restated {
        println!(
            "  {}: {} (the log accounted {})",
            names.symbol(row.symbol),
            row.signed_qty,
            reconcile::logged_exposure(&replayed)
                .get(&row.symbol.0)
                .copied()
                .unwrap_or(0.0)
        );
    }

    // Findings the clear cannot absorb: they will stand at the next boot and
    // latch again, which is the control doing its job.
    let survives: Vec<String> = found
        .findings
        .iter()
        .filter(|finding| {
            finding.stops_opening()
                && !matches!(finding, reconcile::Finding::UnaccountedExposure { .. })
        })
        .map(|finding| format!("{finding:?}"))
        .collect();
    if !survives.is_empty() {
        println!("NOT absorbed — these latch again at the next boot:");
        for line in &survives {
            println!("  {line}");
        }
    }

    if !execute {
        println!("\nreport only; nothing written. Add --execute to clear.");
        return Ok(());
    }

    wal.append(&WalRecord::LatchCleared {
        wall_ts_ms: clock::wall_ms(),
        note: note.to_string(),
        restated_exposure: restated,
        findings: found.lines(),
    })?;
    wal.barrier()?;
    println!(
        "\ncleared: the latch resets and the exposure ledger is restated. The next boot \
         still compares the log to the venue and latches again on anything new."
    );
    Ok(())
}
