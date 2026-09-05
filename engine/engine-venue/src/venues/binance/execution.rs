use crate::wire::Field;
use engine_public::numeric_wire::DecimalField;
use engine_types::numeric::{AssetAmount, AssetId, ExecutionAmounts};
use engine_types::VenueError;
use serde::Deserialize;

#[derive(Default, Deserialize)]
struct Amounts {
    #[serde(default)]
    l: DecimalField,
    #[serde(default, rename = "L")]
    price: DecimalField,
    #[serde(default)]
    n: DecimalField,
    #[serde(default, rename = "N")]
    asset: Field<String>,
}

pub(crate) fn decode(raw: &str) -> Result<(f64, f64, Option<f64>, ExecutionAmounts), VenueError> {
    let row: Amounts =
        crate::wire::raw_object(raw).map_err(|e| VenueError::BadReply(e.to_string()))?;
    let quantity = row.l.required("l")?;
    let price = row.price.required("L")?;
    let fee = row.n.optional("n")?;
    let fee_asset = if fee.is_some() {
        row.asset.required("N")?
    } else {
        ""
    };
    let legacy_fee = if fee_asset == "USDT" {
        fee.as_ref()
            .map(|fee| fee.value.to_f64())
            .transpose()
            .map_err(|e| VenueError::BadReply(e.to_string()))?
    } else {
        None
    };
    Ok((
        row.l.legacy("l")?,
        row.price.legacy("L")?,
        legacy_fee,
        ExecutionAmounts {
            // This adapter accepts only the USDT single-asset account mode.
            settlement_asset: AssetId::Named("USDT".to_owned()),
            quantity,
            price,
            fee: fee.map(|amount| AssetAmount {
                asset: if fee_asset.is_empty() {
                    AssetId::Unknown
                } else {
                    AssetId::Named(fee_asset.to_owned())
                },
                amount,
            }),
        },
    ))
}
