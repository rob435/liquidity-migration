use engine_types::orders::{
    InstrumentCatalog, InstrumentCatalogCacheSnapshot, InstrumentCatalogCheckpoint,
};
use engine_types::VenueError;
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct WireSnapshot {
    base: String,
    pages: Vec<String>,
}
fn bad(detail: impl std::fmt::Display) -> VenueError {
    VenueError::BadReply(detail.to_string())
}
pub(crate) fn encode(
    kind: &str,
    base: &str,
    pages: &[String],
) -> Result<InstrumentCatalogCacheSnapshot, VenueError> {
    if pages.len() > 100 || pages.iter().map(String::len).sum::<usize>() > 64 * 1024 * 1024 {
        return Err(bad("oversized catalog pages"));
    }
    let payload = serde_json::to_vec(&WireSnapshot {
        base: base.into(),
        pages: pages.to_vec(),
    })
    .map_err(bad)?;
    Ok(InstrumentCatalogCacheSnapshot {
        kind: kind.into(),
        payload,
    })
}
pub(crate) fn decode(
    checkpoint: &InstrumentCatalogCheckpoint,
    kind: &str,
    base: &str,
) -> Result<Vec<String>, VenueError> {
    checkpoint.validate_bounds()?;
    if checkpoint.cache.kind != kind {
        return Err(bad("catalog checkpoint belongs to another adapter"));
    }
    let wire: WireSnapshot = serde_json::from_slice(&checkpoint.cache.payload).map_err(bad)?;
    if wire.base != base {
        return Err(bad("catalog checkpoint belongs to another endpoint"));
    }
    if wire.pages.is_empty() || wire.pages.len() > 100 {
        return Err(bad("catalog checkpoint has invalid page count"));
    }
    Ok(wire.pages)
}
/// The rows are a set keyed by symbol, not a sequence: an adapter may
/// enumerate its table in any order, and a checkpoint written by an earlier
/// binary in another order must still restore.
pub(crate) fn check(
    checkpoint: &InstrumentCatalogCheckpoint,
    catalog: InstrumentCatalog,
) -> Result<InstrumentCatalog, VenueError> {
    fn by_symbol<T: Clone>(rows: &[(engine_types::Symbol, T)]) -> Vec<(engine_types::Symbol, T)> {
        let mut rows = rows.to_vec();
        rows.sort_by(|a, b| a.0.cmp(&b.0));
        rows
    }
    if by_symbol(&checkpoint.rules) != by_symbol(&catalog.rules)
        || by_symbol(&checkpoint.specs) != by_symbol(&catalog.specs)
    {
        return Err(bad("catalog checkpoint rows disagree with native metadata"));
    }
    Ok(catalog)
}

pub(crate) fn merge_pages(
    kind: &str,
    previous: Vec<String>,
    fresh: Vec<String>,
) -> Result<Vec<String>, VenueError> {
    use serde_json::value::RawValue;
    type Object = std::collections::BTreeMap<String, Box<RawValue>>;
    fn object(raw: &str) -> Result<Object, VenueError> {
        serde_json::from_str(raw).map_err(bad)
    }
    fn rows(raw: &str, kind: &str) -> Result<Vec<Box<RawValue>>, VenueError> {
        let root = object(raw)?;
        let (root, field) = if kind == "bybit" {
            (
                object(
                    root.get("result")
                        .ok_or_else(|| bad("missing catalog result"))?
                        .get(),
                )?,
                "list",
            )
        } else {
            (
                root,
                match kind {
                    "binance" => "symbols",
                    "hyperliquid" => "universe",
                    "lighter" => "order_book_details",
                    "mexc" => "data",
                    _ => return Err(bad("unknown catalog adapter")),
                },
            )
        };
        serde_json::from_str(
            root.get(field)
                .ok_or_else(|| bad("missing catalog rows"))?
                .get(),
        )
        .map_err(bad)
    }
    fn name(row: &RawValue, kind: &str) -> Result<String, VenueError> {
        let fields = object(row.get())?;
        let key = if kind == "hyperliquid" {
            "name"
        } else {
            "symbol"
        };
        serde_json::from_str(
            fields
                .get(key)
                .ok_or_else(|| bad("catalog row has no native name"))?
                .get(),
        )
        .map_err(bad)
    }
    let old = previous
        .iter()
        .map(|page| rows(page, kind))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
    let new = fresh
        .iter()
        .map(|page| rows(page, kind))
        .collect::<Result<Vec<_>, _>>()?
        .into_iter()
        .flatten()
        .collect::<Vec<_>>();
    let merged = if kind == "hyperliquid" {
        let mut names = std::collections::BTreeMap::new();
        for (index, row) in old.iter().enumerate() {
            if names.insert(name(row, kind)?, index).is_some() {
                return Err(bad("duplicate native asset name"));
            }
        }
        for (index, row) in new.iter().enumerate() {
            let key = name(row, kind)?;
            if names.get(&key).is_some_and(|old| *old != index)
                || old
                    .get(index)
                    .is_some_and(|old| name(old, kind).ok().as_deref() != Some(key.as_str()))
            {
                return Err(bad("native asset index changed or was reused"));
            }
        }
        let mut merged = old;
        for (index, row) in new.into_iter().enumerate() {
            if index < merged.len() {
                merged[index] = row;
            } else {
                merged.push(row);
            }
        }
        merged
    } else {
        let mut merged = std::collections::BTreeMap::new();
        for row in old {
            let key = name(&row, kind)?;
            if merged.insert(key, row).is_some() {
                return Err(bad("duplicate retained native symbol"));
            }
        }
        let mut seen = std::collections::BTreeSet::new();
        for row in new {
            let key = name(&row, kind)?;
            if !seen.insert(key.clone()) {
                return Err(bad("duplicate fresh native symbol"));
            }
            if kind == "lighter" {
                if let Some(old) = merged.get(&key) {
                    let old = object(old.get())?;
                    let new = object(row.get())?;
                    if old.get("market_id").map(|v| v.get())
                        != new.get("market_id").map(|v| v.get())
                    {
                        return Err(bad("native market index changed"));
                    }
                }
            }
            merged.insert(key, row);
        }
        if kind == "mexc" {
            for (key, row) in &mut merged {
                if !seen.contains(key) {
                    // Local provenance, not a rewritten venue API permission.
                    let mut fields = object(row.get())?;
                    fields.insert(
                        "__lm_retained".into(),
                        RawValue::from_string("true".into()).map_err(bad)?,
                    );
                    *row = RawValue::from_string(serde_json::to_string(&fields).map_err(bad)?)
                        .map_err(bad)?;
                }
            }
        }
        if kind == "lighter" {
            let mut indices = std::collections::BTreeSet::new();
            for row in merged.values() {
                let fields = object(row.get())?;
                let index = fields
                    .get("market_id")
                    .ok_or_else(|| bad("market has no index"))?;
                let index: i64 = serde_json::from_str(index.get()).map_err(bad)?;
                if !indices.insert(index) {
                    return Err(bad("native market index reused"));
                }
            }
        }
        merged.into_values().collect()
    };
    let first = fresh
        .first()
        .ok_or_else(|| bad("fresh catalog has no pages"))?;
    let mut root = object(first)?;
    let raw = RawValue::from_string(serde_json::to_string(&merged).map_err(bad)?).map_err(bad)?;
    if kind == "bybit" {
        let mut result = object(
            root.get("result")
                .ok_or_else(|| bad("catalog result missing"))?
                .get(),
        )?;
        result.insert("list".into(), raw);
        result.insert(
            "nextPageCursor".into(),
            RawValue::from_string("\"\"".into()).map_err(bad)?,
        );
        root.insert(
            "result".into(),
            RawValue::from_string(serde_json::to_string(&result).map_err(bad)?).map_err(bad)?,
        );
    } else {
        root.insert(
            match kind {
                "binance" => "symbols",
                "hyperliquid" => "universe",
                "lighter" => "order_book_details",
                "mexc" => "data",
                _ => return Err(bad("unknown catalog adapter")),
            }
            .into(),
            raw,
        );
    }
    Ok(vec![serde_json::to_string(&root).map_err(bad)?])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_checkpoint_restores_whatever_order_its_rows_were_written_in() {
        // Observed live on 2026-09-08: the mexc engine's WAL held a checkpoint
        // written in hash-map order, the fixed binary rebuilt the rows sorted,
        // and boot refused its own checkpoint.
        let rule = |tick: f64| engine_types::InstrumentRule {
            tick_size: tick,
            qty_step: 1.0,
            min_qty: 1.0,
            min_notional: 0.0,
        };
        let rows = vec![
            ("BTCUSDT".to_string(), rule(0.1)),
            ("ETHUSDT".to_string(), rule(0.01)),
            ("XRPUSDT".to_string(), rule(0.0001)),
        ];
        let mut reversed = rows.clone();
        reversed.reverse();
        let checkpoint = InstrumentCatalogCheckpoint {
            schema_version: 1,
            rules: reversed,
            specs: Vec::new(),
            cache: InstrumentCatalogCacheSnapshot {
                kind: "test".into(),
                payload: Vec::new(),
            },
        };
        let catalog = InstrumentCatalog {
            cache: None,
            rules: rows.clone(),
            specs: Vec::new(),
        };
        check(&checkpoint, catalog).unwrap();

        // A row that changed is still a disagreement.
        let mut changed = rows;
        changed[1].1.tick_size = 0.05;
        let catalog = InstrumentCatalog {
            cache: None,
            rules: changed,
            specs: Vec::new(),
        };
        assert!(check(&checkpoint, catalog).is_err());
    }
    #[test]
    fn native_row_union_retains_missing_symbols_and_lexical_values() {
        for (kind, old, fresh) in [
            (
                "bybit",
                r#"{"result":{"list":[{"symbol":"BTC","tick":"0.1234567890123456789"},{"symbol":"ETH","tick":"1"}],"nextPageCursor":""}}"#,
                r#"{"result":{"list":[{"symbol":"ETH","tick":"2"}],"nextPageCursor":""}}"#,
            ),
            (
                "binance",
                r#"{"symbols":[{"symbol":"BTC","tick":0.1234567890123456789},{"symbol":"ETH","tick":1}]}"#,
                r#"{"symbols":[{"symbol":"ETH","tick":2}]}"#,
            ),
            (
                "mexc",
                r#"{"data":[{"symbol":"BTC","tick":0.1234567890123456789},{"symbol":"ETH","tick":1}]}"#,
                r#"{"data":[{"symbol":"ETH","tick":2}]}"#,
            ),
            (
                "lighter",
                r#"{"order_book_details":[{"symbol":"BTC","market_id":7,"tick":0.1234567890123456789},{"symbol":"ETH","market_id":9,"tick":1}]}"#,
                r#"{"order_book_details":[{"symbol":"ETH","market_id":9,"tick":2}]}"#,
            ),
            (
                "hyperliquid",
                r#"{"universe":[{"name":"ETH","tick":1},{"name":"BTC","tick":0.1234567890123456789}]}"#,
                r#"{"universe":[{"name":"ETH","tick":2}]}"#,
            ),
        ] {
            let merged = merge_pages(kind, vec![old.into()], vec![fresh.into()]).unwrap();
            assert_eq!(merged.len(), 1);
            assert!(
                merged[0].contains("0.1234567890123456789"),
                "{kind} lost retained exact precision"
            );
            assert!(merged[0].contains("BTC"));
            assert!(merged[0].contains("ETH"));
            assert_eq!(
                merge_pages(kind, merged.clone(), vec![fresh.into()]).unwrap(),
                merged,
                "repeat refresh accumulated old generations"
            );
        }
    }
    #[test]
    fn native_index_changes_and_reuse_are_refused() {
        for (kind, old, fresh) in [
            (
                "hyperliquid",
                r#"{"universe":[{"name":"BTC"},{"name":"ETH"}]}"#,
                r#"{"universe":[{"name":"ETH"}]}"#,
            ),
            (
                "lighter",
                r#"{"order_book_details":[{"symbol":"BTC","market_id":7}]}"#,
                r#"{"order_book_details":[{"symbol":"BTC","market_id":8}]}"#,
            ),
            (
                "lighter",
                r#"{"order_book_details":[{"symbol":"BTC","market_id":7}]}"#,
                r#"{"order_book_details":[{"symbol":"ETH","market_id":7}]}"#,
            ),
        ] {
            assert!(
                merge_pages(kind, vec![old.into()], vec![fresh.into()]).is_err(),
                "{kind} reused a native ID"
            );
        }
    }
}
