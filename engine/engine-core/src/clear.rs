//! Operator reconciliation restates verified native exposure under the WAL lock.
//! Owned inventory must agree first; missing fills require history recovery.

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
    let chosen = assembly::venue_name(&settings.venue)?;
    let mut venue = assembly::venue(chosen, names.symbols.clone())?;
    let who = venue.account_identity().await?;
    println!("log     {}", settings.wal_path.display());
    println!("account {} on {} ({})", who.user_id, who.venue, who.realm);

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
    let mut quantity_steps: Vec<Option<f64>> = vec![None; names.symbols.len()];
    let mut price_ticks: Vec<Option<f64>> = vec![None; names.symbols.len()];
    match venue.instrument_rules().await {
        Ok(rules) => {
            for (name, rule) in rules {
                if let Some(at) = names.symbols.iter().position(|n| *n == name) {
                    quantity_steps[at] = Some(rule.qty_step);
                    price_ticks[at] = Some(rule.tick_size);
                }
            }
        }
        Err(e) => println!("no instrument rules ({e}); judging with the smallest tolerance"),
    }

    let orders = LedgerOfOrders::try_from_records(&replayed)?;
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
        |id| quantity_steps.get(id.0 as usize).copied().flatten(),
        |id| price_ticks.get(id.0 as usize).copied().flatten(),
    )?;

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
    let restated = restated_positions(&account)?;
    let specs = venue
        .instrument_specs()
        .await?
        .into_iter()
        .filter_map(|(name, spec)| {
            names
                .symbols
                .iter()
                .position(|symbol| *symbol == name)
                .map(|index| (SymbolId(index as u16), spec))
        })
        .collect();
    validate_owned_restated(&replayed, &restated, &specs)?;
    println!("restating exposure over {} symbol(s):", restated.len());
    for row in &restated {
        println!(
            "  {}: {} (the log accounted {})",
            names.symbol(row.symbol),
            row.signed_qty,
            reconcile::logged_exposure(&replayed)?
                .get(&row.symbol)
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

    if !append_clear(
        &mut wal,
        &replayed,
        WalRecord::LatchCleared {
            wall_ts_ms: clock::wall_ms(),
            note: note.to_string(),
            restated_exposure: restated,
            findings: found.lines(),
        },
    )? {
        println!("already applied: identical reconciliation and native exposure");
        return Ok(());
    }
    println!(
        "\ncleared: the latch resets and the exposure ledger is restated. The next boot \
         still compares the log to the venue and latches again on anything new."
    );
    Ok(())
}

fn restated_positions(
    account: &engine_types::AccountView,
) -> Result<Vec<SymbolTotal>, engine_types::numeric::ExactError> {
    account
        .positions
        .iter()
        .map(|position| {
            let quantity = position.quantity()?;
            let signed = match position.side {
                Side::Buy => quantity,
                Side::Sell => -quantity,
            };
            let signed_qty = signed.to_f64()?;
            Ok(SymbolTotal {
                symbol: position.symbol,
                signed_qty,
                exact_signed_qty: Some(engine_types::numeric::ExactNumber::derived(signed)),
            })
        })
        .collect::<Result<_, engine_types::numeric::ExactError>>()
}

fn validate_owned_restated(
    records: &[WalRecord],
    restated: &[SymbolTotal],
    specs: &std::collections::BTreeMap<SymbolId, engine_types::numeric::ExactInstrumentSpec>,
) -> Result<(), String> {
    let adoption = crate::legacy_quantity::plan(records, specs, 0)?;
    let attribution = crate::legacy_quantity::Replay::new(records, adoption.as_ref())?.finish()?;
    let mut owned = std::collections::BTreeMap::<SymbolId, engine_types::numeric::Exact>::new();
    for row in attribution.snapshot().positions {
        *owned.entry(row.symbol).or_default() += row.signed_qty;
    }
    for (symbol, quantity) in owned {
        let native = restated
            .iter()
            .find(|row| row.symbol == symbol)
            .map(|row| row.exact_quantity())
            .transpose()
            .map_err(|e| e.to_string())?
            .unwrap_or_default();
        if native != quantity {
            return Err(format!("symbol {} still has owned fills missing from the account; recover execution history before clearing",symbol.0));
        }
    }
    Ok(())
}

fn append_clear<W: Wal>(
    wal: &mut W,
    records: &[WalRecord],
    record: WalRecord,
) -> Result<bool, engine_types::WalError> {
    if let (
        Some(WalRecord::LatchCleared {
            note: prior_note,
            restated_exposure: prior,
            ..
        }),
        WalRecord::LatchCleared {
            note,
            restated_exposure,
            ..
        },
    ) = (records.last(), &record)
    {
        if prior_note == note && prior == restated_exposure {
            return Ok(false);
        }
    }
    wal.append(&record)?;
    wal.barrier()?;
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::numeric::ExactNumber;
    use engine_types::risk::PositionAmounts;
    use engine_types::{AccountView, PositionView};

    #[test]
    fn reconciliation_clear_preserves_native_quantity_through_replay() {
        for (side, value) in [
            (Side::Buy, "1.7"),
            (Side::Sell, "9007199254740993"),
            (Side::Buy, "0.000000000000000001"),
        ] {
            let quantity = ExactNumber::venue_decimal(value).unwrap();
            let account = AccountView {
                exact_amounts: None,
                equity_usdt: 1000.0,
                available_usdt: 1000.0,
                observed_ns: 1,
                positions: vec![PositionView {
                    exact_amounts: Some(Box::new(PositionAmounts {
                        quantity: quantity.clone(),
                        entry_price: ExactNumber::venue_decimal("100").unwrap(),
                    })),
                    symbol: SymbolId(0),
                    side,
                    qty: quantity.value.to_f64().unwrap(),
                    entry_px: 100.0,
                    stop_attached: true,
                    stop_px: 90.0,
                    exact_stop_px: None,
                    leverage: None,
                }],
            };
            let rows = restated_positions(&account).unwrap();
            let expected = if side == Side::Buy {
                quantity.value
            } else {
                -quantity.value
            };
            assert_eq!(rows[0].exact_quantity().unwrap(), expected);
            let record = WalRecord::LatchCleared {
                wall_ts_ms: 2,
                note: "verified native close".into(),
                restated_exposure: rows,
                findings: vec![],
            };
            let bytes = serde_json::to_vec(&record).unwrap();
            let restored: WalRecord = serde_json::from_slice(&bytes).unwrap();
            assert_eq!(
                reconcile::physical_exposure(&[restored]).unwrap()[&SymbolId(0)],
                expected
            );
        }
    }
    #[test]
    fn reconciliation_clear_cannot_absorb_an_owned_missing_close() {
        let baseline = |rows: serde_json::Value| -> WalRecord {
            serde_json::from_value(serde_json::json!({
            "kind":"segment_base","wall_ts_ms":1,"strategies":["long","other"],"symbols":["BTCUSDT"],
            "may_open":true,"control_anchors":[],"open_orders":[],"attribution":rows,"logged_exposure":[],"intended_stops":[]
        })).unwrap()
        };
        let owned = baseline(
            serde_json::json!([{"strategy":0,"symbol":0,"signed_qty":1.7000000000000002}]),
        );
        let mut spec = crate::tests::shared_sleeves::spec();
        spec.qty_step = Some(engine_types::numeric::Exact::parse_decimal("0.1").unwrap());
        let specs = std::collections::BTreeMap::from([(SymbolId(0), spec)]);
        let native = |qty: &str| SymbolTotal {
            symbol: SymbolId(0),
            signed_qty: qty.parse().unwrap(),
            exact_signed_qty: Some(ExactNumber::venue_decimal(qty).unwrap()),
        };
        validate_owned_restated(std::slice::from_ref(&owned), &[native("1.7")], &specs).unwrap();
        assert!(validate_owned_restated(std::slice::from_ref(&owned), &[], &specs).is_err());
        assert!(validate_owned_restated(&[owned], &[native("1.6")], &specs).is_err());
        let opposing = baseline(
            serde_json::json!([{"strategy":0,"symbol":0,"signed_qty":2.0},{"strategy":1,"symbol":0,"signed_qty":-2.0}]),
        );
        validate_owned_restated(&[opposing], &[], &specs).unwrap();
        let orphan = baseline(serde_json::json!([]));
        validate_owned_restated(&[orphan], &[], &specs).unwrap();
    }

    #[test]
    fn reconciliation_clear_retry_after_barrier_preserves_wal_bytes() {
        let path =
            std::env::temp_dir().join(format!("engine-clear-retry-{}.wal", std::process::id()));
        let _ = std::fs::remove_file(&path);
        let (mut wal, _) = engine_wal::open_current(&path).unwrap();
        let record = |wall_ts_ms| WalRecord::LatchCleared {
            wall_ts_ms,
            note: "historical stop exec-123".into(),
            restated_exposure: vec![],
            findings: vec!["historical physical residue".into()],
        };
        assert!(append_clear(&mut wal, &[], record(1)).unwrap());
        drop(wal);
        let before = std::fs::read(&path).unwrap();
        let (mut wal, records) = engine_wal::open_current(&path).unwrap();
        let records = records.into_iter().map(|(_, row)| row).collect::<Vec<_>>();
        assert!(!append_clear(&mut wal, &records, record(2)).unwrap());
        drop(wal);
        assert_eq!(std::fs::read(&path).unwrap(), before);
        std::fs::remove_file(&path).unwrap();
    }
}
