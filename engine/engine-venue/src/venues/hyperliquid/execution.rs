use crate::wire::{Field, IntegerField};
use engine_public::numeric_wire::DecimalField;
use engine_types::numeric::{AssetAmount, AssetId, ExecutionAmounts};
use engine_types::{Side, VenueError, VenueExecution};
use serde::Deserialize;
#[cfg(test)]
use serde_json::Value;

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct ExecutionRow {
    coin: Field<String>,
    tid: IntegerField,
    cloid: Field<String>,
    side: Field<String>,
    sz: DecimalField,
    px: DecimalField,
    fee: DecimalField,
    fee_token: Field<String>,
    crossed: Field<bool>,
    time: IntegerField,
}

#[cfg(test)]
pub(crate) fn decode(row: &Value) -> Result<VenueExecution, VenueError> {
    decode_raw(&row.to_string())
}

pub(crate) fn decode_raw(raw: &str) -> Result<VenueExecution, VenueError> {
    let row: ExecutionRow = crate::wire::raw_object(raw)
        .map_err(|e| VenueError::BadReply(format!("execution row: {e}")))?;
    let side = match row.side.required("side")? {
        "A" | "a" => Side::Sell,
        "B" | "b" => Side::Buy,
        other => {
            return Err(VenueError::BadReply(format!(
                "side is {other:?}, and this venue writes A for ask or B for bid"
            )))
        }
    };
    let amount = row.fee.required("fee")?;
    let amounts = ExecutionAmounts {
        settlement_asset: AssetId::Unknown,
        quantity: row.sz.required("sz")?,
        price: row.px.required("px")?,
        fee: Some(AssetAmount {
            asset: match row.fee_token.text() {
                "" => AssetId::Unknown,
                asset => AssetId::Named(asset.to_owned()),
            },
            amount,
        }),
    };
    Ok(VenueExecution {
        exec_id: row.tid.required("tid")?.to_string(),
        client_order_id: super::cloid::from_cloid(row.cloid.text()).unwrap_or_default(),
        symbol: super::assets::symbol_of(row.coin.required("coin")?),
        side,
        qty: row.sz.legacy("sz")?,
        px: row.px.legacy("px")?,
        fee: Some(row.fee.legacy("fee")?),
        amounts: Some(amounts),
        is_maker: !row.crossed.0.unwrap_or(true),
        forced_close: None,
        venue_ts_ms: row.time.required("time")?,
    })
}
