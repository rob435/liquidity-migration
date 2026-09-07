//! Instrument constraint decoding, independent of event delivery and execution.
use super::tape::TapeError;
use engine_types::numeric::{AssetId, Exact, ExactInstrumentSpec, PricePrecision};
use engine_types::orders::InstrumentCatalog;
use engine_types::InstrumentRule;
use serde_json::Value;
use std::fs::File;
use std::io::Read;
use std::path::Path;
use std::process::{Command, Stdio};

// ------------------------------------------------------- instruments

/// Recorder instrument rows retain decimal constraints; legacy rules are
/// projections of the same catalog. Missing assets and bounds stay unknown.
pub fn read_instruments(path: &Path) -> Result<InstrumentCatalog, TapeError> {
    let mut text = String::new();
    if path.extension().is_some_and(|ext| ext == "zst") {
        let output = Command::new("zstd")
            .arg("-dc")
            .arg("--")
            .arg(path)
            .stderr(Stdio::inherit())
            .output()
            .map_err(|source| TapeError::Zstd {
                path: path.display().to_string(),
                source,
            })?;
        if !output.status.success() {
            return Err(TapeError::Malformed {
                line: 1,
                detail: format!("instrument decompression failed: {}", output.status),
            });
        }
        text = String::from_utf8(output.stdout).map_err(|e| TapeError::Malformed {
            line: 1,
            detail: e.to_string(),
        })?;
    } else {
        File::open(path)?.read_to_string(&mut text)?;
    }
    let malformed = |detail: String| TapeError::Malformed { line: 1, detail };
    let payload: Value = serde_json::from_str(text.trim()).map_err(|e| malformed(e.to_string()))?;
    let kind = payload.get("kind").and_then(Value::as_str).unwrap_or("");
    if kind == "instrument_catalog_v1" {
        let specs: Vec<(String, ExactInstrumentSpec)> = serde_json::from_value(
            payload
                .get("specs")
                .cloned()
                .ok_or_else(|| malformed("instrument_catalog_v1 lacks specs".into()))?,
        )
        .map_err(|e| malformed(e.to_string()))?;
        let mut rules = Vec::new();
        for (symbol, spec) in &specs {
            if symbol.is_empty()
                || spec.native_symbol != *symbol
                || rules.iter().any(|(name, _)| name == symbol)
            {
                return Err(malformed(
                    "instrument symbol must be unique and match its native identity".into(),
                ));
            }
            let required = |name: &str, value: &Option<Exact>| -> Result<f64, TapeError> {
                let value = value
                    .as_ref()
                    .filter(|v| v.is_positive())
                    .ok_or_else(|| malformed(format!("{symbol}: missing positive {name}")))?;
                value.to_f64().map_err(|e| malformed(e.to_string()))
            };
            rules.push((
                symbol.clone(),
                InstrumentRule {
                    tick_size: required("tick_size", &spec.tick_size)?,
                    qty_step: required("qty_step", &spec.qty_step)?,
                    min_qty: required("min_qty", &spec.min_qty)?,
                    min_notional: spec
                        .min_notional
                        .as_ref()
                        .map(|n| n.to_f64().map_err(|e| malformed(e.to_string())))
                        .transpose()?
                        .unwrap_or(0.0),
                },
            ));
        }
        return Ok(InstrumentCatalog {
            cache: None,
            rules,
            specs,
        });
    }
    if kind != "instruments_snapshot" {
        return Err(malformed(format!(
            "expected an instruments_snapshot payload, found kind {kind:?}"
        )));
    }
    let rows = payload
        .get("rows")
        .and_then(Value::as_array)
        .ok_or_else(|| malformed("instruments_snapshot has no rows".to_string()))?;
    let mut out = InstrumentCatalog {
        cache: None,
        rules: Vec::with_capacity(rows.len()),
        specs: Vec::with_capacity(rows.len()),
    };
    for row in rows {
        let Some(symbol) = row.get("symbol").and_then(Value::as_str) else {
            continue;
        };
        let (Some(price_filter), Some(lot_filter)) =
            (row.get("priceFilter"), row.get("lotSizeFilter"))
        else {
            tracing::warn!(
                symbol,
                "instrument has no price or lot filter; not tradable"
            );
            continue;
        };
        let field = |obj: &Value, name: &str| -> Result<Exact, TapeError> {
            let v = obj
                .get(name)
                .ok_or_else(|| malformed(format!("{symbol}: instrument lacks {name}")))?;
            match v {
                Value::String(text) => Exact::parse_decimal(text.trim()),
                // Numeric JSON fields retain this reader's binary64 input model.
                Value::Number(number) => {
                    Exact::from_legacy_f64(number.as_f64().ok_or_else(|| {
                        malformed(format!("{symbol}: {name} is not a finite binary64 number"))
                    })?)
                }
                _ => return Err(malformed(format!("{symbol}: {name} is not a number"))),
            }
            .map_err(|error| malformed(format!("{symbol}: {name}: {error}")))
        };
        let optional = |obj: &Value, name: &str| -> Result<Option<Exact>, TapeError> {
            match obj.get(name) {
                None | Some(Value::Null) => Ok(None),
                _ => field(obj, name).map(Some),
            }
        };
        let projection = |value: &Exact| {
            value
                .to_f64()
                .map_err(|error| malformed(format!("{symbol}: {error}")))
        };
        let asset = |name| {
            row.get(name)
                .and_then(Value::as_str)
                .filter(|name| !name.is_empty())
                .map(|name| AssetId::Named(name.to_owned()))
                .unwrap_or(AssetId::Unknown)
        };
        let tick_size = field(price_filter, "tickSize")?;
        let qty_step = field(lot_filter, "qtyStep")?;
        let min_qty = field(lot_filter, "minOrderQty")?;
        let min_notional = optional(lot_filter, "minNotionalValue")?;
        if !tick_size.is_positive() || !qty_step.is_positive() {
            tracing::warn!(symbol, "instrument has a zero tick or step");
            continue;
        }
        out.rules.push((
            symbol.to_string(),
            InstrumentRule {
                tick_size: projection(&tick_size)?,
                qty_step: projection(&qty_step)?,
                min_qty: projection(&min_qty)?,
                min_notional: min_notional
                    .as_ref()
                    .map(projection)
                    .transpose()?
                    .unwrap_or(0.0),
            },
        ));
        out.specs.push((
            symbol.to_string(),
            ExactInstrumentSpec {
                native_symbol: symbol.to_string(),
                base_asset: asset("baseCoin"),
                quote_asset: asset("quoteCoin"),
                settlement_asset: asset("settleCoin"),
                tick_size: Some(tick_size),
                min_price: optional(price_filter, "minPrice")?,
                max_price: optional(price_filter, "maxPrice")?,
                price_precision: PricePrecision::Tick,
                qty_step: Some(qty_step.clone()),
                min_qty: Some(min_qty.clone()),
                market_qty_step: Some(qty_step),
                market_min_qty: Some(min_qty),
                max_qty: optional(lot_filter, "maxOrderQty")?,
                max_market_qty: optional(lot_filter, "maxMktOrderQty")?,
                min_notional,
                contract_multiplier: None,
                fee_assets: None,
                fee_step: None,
            },
        ));
    }
    Ok(out)
}
