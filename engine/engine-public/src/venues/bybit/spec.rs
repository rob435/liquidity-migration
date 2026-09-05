use crate::numeric_wire::{decode_object, DecimalField};
use engine_types::numeric::{AssetId, Exact, ExactInstrumentSpec, PricePrecision};
use engine_types::{Symbol, VenueError};
use serde::Deserialize;
use serde_json::value::RawValue;

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Reply {
    ret_code: i64,
    #[serde(default)]
    ret_msg: String,
    #[serde(default)]
    result: Option<Box<RawValue>>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Page {
    list: Vec<Box<RawValue>>,
    #[serde(default)]
    next_page_cursor: String,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Instrument {
    symbol: String,
    #[serde(default)]
    base_coin: Option<String>,
    #[serde(default)]
    quote_coin: Option<String>,
    #[serde(default)]
    settle_coin: Option<String>,
    #[serde(default)]
    price_filter: Option<Box<RawValue>>,
    #[serde(default)]
    lot_size_filter: Option<Box<RawValue>>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct PriceFilter {
    tick_size: DecimalField,
    #[serde(default)]
    min_price: DecimalField,
    #[serde(default)]
    max_price: DecimalField,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct LotFilter {
    qty_step: DecimalField,
    min_order_qty: DecimalField,
    #[serde(default)]
    max_order_qty: DecimalField,
    #[serde(default)]
    max_mkt_order_qty: DecimalField,
    #[serde(default)]
    min_notional_value: DecimalField,
}
fn asset(name: Option<String>) -> AssetId {
    name.filter(|name| !name.is_empty())
        .map(AssetId::Named)
        .unwrap_or(AssetId::Unknown)
}

pub fn parse_page(raw: &str) -> Result<(Vec<(Symbol, ExactInstrumentSpec)>, String), VenueError> {
    let reply: Reply = decode_object(raw).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if reply.ret_code != 0 {
        return Err(VenueError::Rejected {
            code: reply.ret_code,
            message: reply.ret_msg,
        });
    }
    let result = reply
        .result
        .ok_or_else(|| VenueError::BadReply("instrument reply carries no result".into()))?;
    let page: Page =
        decode_object(result.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
    let mut out = Vec::with_capacity(page.list.len());
    for raw in page.list {
        let row: Instrument =
            decode_object(raw.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
        let (Some(price), Some(lot)) = (row.price_filter, row.lot_size_filter) else {
            continue;
        };
        let price: PriceFilter =
            decode_object(price.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
        let lot: LotFilter =
            decode_object(lot.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
        let tick = price.tick_size.required("tickSize")?.value;
        let step = lot.qty_step.required("qtyStep")?.value;
        if !tick.is_positive() || !step.is_positive() {
            continue;
        }
        let min_qty = lot.min_order_qty.required("minOrderQty")?.value;
        out.push((
            row.symbol.clone(),
            ExactInstrumentSpec {
                native_symbol: row.symbol,
                base_asset: asset(row.base_coin),
                quote_asset: asset(row.quote_coin),
                settlement_asset: asset(row.settle_coin),
                tick_size: Some(tick),
                min_price: price.min_price.optional("minPrice")?.map(|n| n.value),
                max_price: price.max_price.optional("maxPrice")?.map(|n| n.value),
                price_precision: PricePrecision::Tick,
                qty_step: Some(step.clone()),
                min_qty: Some(min_qty.clone()),
                market_qty_step: Some(step),
                market_min_qty: Some(min_qty),
                max_qty: lot
                    .max_order_qty
                    .optional("maxOrderQty")?
                    .map(|number| number.value),
                max_market_qty: lot
                    .max_mkt_order_qty
                    .optional("maxMktOrderQty")?
                    .map(|number| number.value),
                min_notional: lot
                    .min_notional_value
                    .optional("minNotionalValue")?
                    .map(|number| number.value),
                contract_multiplier: Some(Exact::one()),
                fee_assets: None,
                fee_step: None,
            },
        ));
    }
    Ok((out, page.next_page_cursor))
}
