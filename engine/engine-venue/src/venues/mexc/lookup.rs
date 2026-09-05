use super::contracts::Contract;
use engine_public::numeric_wire::{decode_object, DecimalField, IntegerField};
use engine_types::numeric::ExactNumber;
use engine_types::orders::{OrderLookup, TerminalOrderStatus};
use engine_types::VenueError;
use serde::Deserialize;
use serde_json::value::RawValue;

#[derive(Deserialize)]
struct Reply {
    success: bool,
    code: i64,
    #[serde(default)]
    message: String,
    #[serde(default)]
    data: Option<Box<RawValue>>,
}
#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Row {
    symbol: String,
    external_oid: String,
    order_id: crate::wire::Id,
    state: IntegerField,
    deal_vol: DecimalField,
}

pub(crate) fn parse(
    raw: &str,
    symbol: &str,
    client_id: &str,
    contract: &Contract,
) -> Result<OrderLookup, VenueError> {
    let reply: Reply = decode_object(raw).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if !reply.success || reply.code != 0 {
        return Err(VenueError::Rejected {
            code: reply.code,
            message: reply.message,
        });
    }
    let Some(data) = reply.data else {
        return Ok(crate::order_lookup::unknown(
            "MEXC returned no retained order",
        ));
    };
    let decoded: Row =
        decode_object(data.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
    if decoded.symbol != contract.venue_symbol || decoded.external_oid != client_id {
        return Err(VenueError::BadReply(
            "order lookup identity differs from request".into(),
        ));
    }
    let filled = ExactNumber::derived(
        decoded.deal_vol.required("dealVol")?.value * &contract.exact_contract_size.value,
    );
    let row = crate::order_lookup::row(
        symbol.into(),
        decoded.external_oid,
        decoded.order_id.into_text(),
        filled,
    )?;
    Ok(match decoded.state.required("state")? {
        1 | 2 => OrderLookup::Working(row),
        3 => crate::order_lookup::terminal(TerminalOrderStatus::Filled, row),
        4 => crate::order_lookup::terminal(TerminalOrderStatus::Cancelled, row),
        5 => crate::order_lookup::terminal(TerminalOrderStatus::Rejected, row),
        state => crate::order_lookup::unknown(format!("unknown MEXC order state {state}")),
    })
}
