use crate::numeric_wire::{decode_object, DecimalField};
use engine_types::numeric::{AssetId, Exact, ExactInstrumentSpec, PricePrecision};
use engine_types::{Symbol, VenueError};
use serde::Deserialize;
use serde_json::value::RawValue;

#[derive(Deserialize)]
struct Reply {
    symbols: Vec<Box<RawValue>>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Instrument {
    symbol: String,
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    contract_type: Option<String>,
    #[serde(default)]
    base_asset: Option<String>,
    #[serde(default)]
    quote_asset: Option<String>,
    #[serde(default)]
    margin_asset: Option<String>,
    #[serde(default)]
    filters: Vec<Box<RawValue>>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Filter {
    filter_type: String,
    #[serde(default)]
    tick_size: DecimalField,
    #[serde(default)]
    min_price: DecimalField,
    #[serde(default)]
    max_price: DecimalField,
    #[serde(default)]
    step_size: DecimalField,
    #[serde(default)]
    min_qty: DecimalField,
    #[serde(default)]
    max_qty: DecimalField,
    #[serde(default)]
    notional: DecimalField,
}

pub fn parse(raw: &str) -> Result<Vec<(Symbol, ExactInstrumentSpec)>, VenueError> {
    let reply: Reply = decode_object(raw).map_err(|e| VenueError::BadReply(e.to_string()))?;
    let mut out = Vec::with_capacity(reply.symbols.len());
    for raw in reply.symbols {
        let Ok(row) = decode_object::<Instrument>(raw.get()) else {
            continue;
        };
        if row.status.as_deref() != Some("TRADING")
            || row.contract_type.as_deref() != Some("PERPETUAL")
            || row.quote_asset.as_deref() != Some("USDT")
            || row.margin_asset.as_deref() != Some("USDT")
        {
            continue;
        }
        let filters = row
            .filters
            .iter()
            .filter_map(|raw| decode_object::<Filter>(raw.get()).ok())
            .collect::<Vec<_>>();
        let filter = |kind: &str| filters.iter().find(|filter| filter.filter_type == kind);
        let (Some(price), Some(lot), Some(market), Some(notional)) = (
            filter("PRICE_FILTER"),
            filter("LOT_SIZE"),
            filter("MARKET_LOT_SIZE"),
            filter("MIN_NOTIONAL"),
        ) else {
            continue;
        };
        let (
            Ok(tick),
            Ok(step),
            Ok(min),
            Ok(max),
            Ok(market_step),
            Ok(market_min),
            Ok(market_max),
            Ok(notional),
        ) = (
            price.tick_size.required("tickSize"),
            lot.step_size.required("stepSize"),
            lot.min_qty.required("minQty"),
            lot.max_qty.required("maxQty"),
            market.step_size.required("stepSize"),
            market.min_qty.required("minQty"),
            market.max_qty.required("maxQty"),
            notional.notional.required("notional"),
        )
        else {
            continue;
        };
        if !tick.value.is_positive()
            || !step.value.is_positive()
            || !min.value.is_positive()
            || max.value < min.value
            || !market_step.value.is_positive()
            || !market_min.value.is_positive()
            || market_max.value < market_min.value
            || !notional.value.is_positive()
        {
            continue;
        }
        let common = if step
            .value
            .is_multiple_of(&market_step.value)
            .unwrap_or(false)
        {
            &step.value
        } else if market_step
            .value
            .is_multiple_of(&step.value)
            .unwrap_or(false)
        {
            &market_step.value
        } else {
            continue;
        };
        let common_min = min
            .value
            .clone()
            .max(market_min.value.clone())
            .ceil_to(common)
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        if common_min > max.value.clone().min(market_max.value.clone()) {
            continue;
        }
        out.push((
            row.symbol.clone(),
            ExactInstrumentSpec {
                native_symbol: row.symbol,
                base_asset: row
                    .base_asset
                    .filter(|name| !name.is_empty())
                    .map(AssetId::Named)
                    .unwrap_or(AssetId::Unknown),
                quote_asset: AssetId::Named("USDT".into()),
                settlement_asset: AssetId::Named("USDT".into()),
                tick_size: Some(tick.value),
                min_price: price
                    .min_price
                    .optional("minPrice")?
                    .map(|n| n.value)
                    .filter(|value| !value.is_zero()),
                max_price: price
                    .max_price
                    .optional("maxPrice")?
                    .map(|n| n.value)
                    .filter(|value| !value.is_zero()),
                price_precision: PricePrecision::Tick,
                qty_step: Some(step.value),
                min_qty: Some(min.value),
                max_qty: Some(max.value),
                market_qty_step: Some(market_step.value),
                market_min_qty: Some(market_min.value),
                max_market_qty: Some(market_max.value),
                min_notional: Some(notional.value),
                contract_multiplier: Some(Exact::one()),
                fee_assets: None,
                fee_step: None,
            },
        ));
    }
    if out.is_empty() {
        return Err(VenueError::BadReply(
            "exchangeInfo listed no readable trading symbols".into(),
        ));
    }
    Ok(out)
}
