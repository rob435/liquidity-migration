use super::contracts::Contracts;
use crate::wire::{Field, Id, IntegerField, RawField};
use engine_public::numeric_wire::DecimalField;
use engine_types::numeric::{AssetAmount, ExactNumber, ExecutionAmounts};
use engine_types::{VenueError, VenueExecution};
use serde::Deserialize;

#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
struct Deal {
    symbol: Field<String>,
    side: IntegerField,
    id: Field<Id>,
    external_oid: RawField<Option<String>>,
    vol: DecimalField,
    price: DecimalField,
    fee: DecimalField,
    taker: Field<bool>,
    timestamp: IntegerField,
}

#[derive(Default, Deserialize)]
#[serde(default)]
pub(crate) struct HistoryReply {
    success: Field<bool>,
    code: Field<i64>,
    message: Field<String>,
    data: RawField<Box<serde_json::value::RawValue>>,
}
impl HistoryReply {
    pub(crate) fn executions(
        self,
        contracts: &Contracts,
    ) -> Result<(Vec<VenueExecution>, usize), VenueError> {
        let code = self.code.0.unwrap_or(-1);
        if self.success.0 != Some(true) || code != 0 {
            return Err(VenueError::Rejected {
                code,
                message: self.message.0.unwrap_or_else(|| "(no message)".to_owned()),
            });
        }
        let raw = self
            .data
            .0
            .ok_or_else(|| VenueError::BadReply("a successful reply carried no data".into()))?;
        let rows: Vec<Box<serde_json::value::RawValue>> = if raw.get().starts_with('[') {
            serde_json::from_str(raw.get())
        } else {
            #[derive(Deserialize)]
            struct Page {
                #[serde(rename = "resultList")]
                rows: Vec<Box<serde_json::value::RawValue>>,
            }
            serde_json::from_str::<Page>(raw.get()).map(|page| page.rows)
        }
        .map_err(|e| VenueError::BadReply(format!("order deals carried no rows: {e}")))?;
        let count = rows.len();
        let mut executions = Vec::with_capacity(count);
        for raw in rows {
            let row: Deal = crate::wire::raw_object(raw.get())
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            let venue_symbol = row.symbol.required("symbol")?;
            let symbol = contracts.symbol_of(venue_symbol).ok_or_else(|| {
                VenueError::BadReply(format!("execution names unknown contract {venue_symbol}"))
            })?;
            let contract = contracts.any(symbol).ok_or_else(|| {
                VenueError::BadReply(format!(
                    "contract metadata vanished for execution in {symbol}"
                ))
            })?;
            let side_raw = row.side.required("side")?;
            let (side, _) = super::parse::side_of(side_raw).ok_or_else(|| {
                VenueError::BadReply(format!(
                    "execution in {venue_symbol} has unknown side {side_raw}"
                ))
            })?;
            let exec_id = row
                .id
                .0
                .map(Id::into_text)
                .filter(|id| !id.trim().is_empty())
                .ok_or_else(|| {
                    VenueError::BadReply(format!("execution in {venue_symbol} has no readable id"))
                })?;
            // Missing and null externalOid are valid, but an explicit non-string is not.
            let fields: std::collections::BTreeMap<String, Box<serde_json::value::RawValue>> =
                serde_json::from_str(raw.get()).map_err(|e| VenueError::BadReply(e.to_string()))?;
            if fields.contains_key("externalOid") && row.external_oid.0.is_none() {
                return Err(VenueError::BadReply(format!(
                    "execution {exec_id} in {venue_symbol} has a non-string externalOid"
                )));
            }
            let quantity = ExactNumber::derived(
                &row.vol.required("vol")?.value * &contract.exact_contract_size.value,
            );
            let qty = quantity
                .value
                .to_f64()
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            let px = row.price.legacy("price")?;
            let fee = row.fee.optional("fee")?;
            let legacy_fee = fee
                .as_ref()
                .map(|fee| fee.value.to_f64())
                .transpose()
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            let venue_ts_ms = row.timestamp.required("timestamp")?;
            if qty <= 0.0 || px <= 0.0 || venue_ts_ms <= 0 {
                return Err(VenueError::BadReply(format!("execution {exec_id} in {venue_symbol} has non-positive quantity, price, or timestamp")));
            }
            let amounts = ExecutionAmounts {
                settlement_asset: contract.settlement_asset.clone(),
                quantity,
                price: row.price.required("price")?,
                fee: fee.map(|amount| AssetAmount {
                    asset: contract.settlement_asset.clone(),
                    amount,
                }),
            };
            executions.push(VenueExecution {
                exec_id,
                client_order_id: row.external_oid.0.flatten().unwrap_or_default(),
                symbol: symbol.to_owned(),
                side,
                qty,
                px,
                fee: legacy_fee,
                amounts: Some(amounts),
                is_maker: !row.taker.0.ok_or_else(|| {
                    VenueError::BadReply(format!(
                        "execution in {venue_symbol} has no boolean taker flag"
                    ))
                })?,
                forced_close: None,
                venue_ts_ms,
            });
        }
        Ok((executions, count))
    }
}
