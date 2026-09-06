use engine_public::numeric_wire::DecimalField;
use engine_types::numeric::{AssetAmount, AssetId, ExecutionAmounts};
use engine_types::{ForcedClose, Side, VenueError, VenueExecution};
use serde::Deserialize;
#[cfg(test)]
use serde_json::Value;

use crate::wire::{Field, IntegerField};

#[derive(Debug, Default, Deserialize)]
#[serde(rename_all = "camelCase", default)]
pub(crate) struct ExecutionRow {
    exec_type: Field<String>,
    exec_id: Field<String>,
    order_link_id: Field<String>,
    symbol: Field<String>,
    side: Field<String>,
    exec_qty: DecimalField,
    exec_price: DecimalField,
    exec_fee: DecimalField,
    fee_currency: Field<String>,
    is_maker: Field<bool>,
    exec_time: IntegerField,
    create_type: Field<String>,
    stop_order_type: Field<String>,
}

impl ExecutionRow {
    #[cfg(test)]
    pub(crate) fn decode(row: &Value) -> Result<Self, VenueError> {
        Self::decode_raw(&row.to_string())
    }
    pub(crate) fn decode_raw(raw: &str) -> Result<Self, VenueError> {
        crate::wire::raw_object(raw)
            .map_err(|e| VenueError::BadReply(format!("execution row: {e}")))
    }
    pub(crate) fn normalized(&self, stream: bool) -> Result<Option<VenueExecution>, VenueError> {
        let exec_type = if stream {
            self.exec_type.required("execType")?
        } else {
            self.exec_type.text()
        };
        if !matches!(exec_type, "Trade" | "AdlTrade" | "BustTrade" | "Settle") {
            return Ok(None);
        }
        let symbol = self.symbol.required("symbol")?;
        let side = match self.side.required("side")? {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            other => {
                return Err(VenueError::BadReply(format!(
                    "execution in {symbol} has an unknown side {other:?}"
                )))
            }
        };
        let exec_id = self.exec_id.required("execId")?;
        if exec_id.is_empty() {
            return Err(VenueError::BadReply(format!(
                "quantity-moving execution in {symbol} has no execId"
            )));
        }
        let quantity = self.exec_qty.required("execQty")?;
        let price = self.exec_price.required("execPrice")?;
        let qty = self.exec_qty.legacy("execQty")?;
        let px = self.exec_price.legacy("execPrice")?;
        let venue_ts_ms = self.exec_time.required("execTime")?;
        if qty <= 0.0 || px <= 0.0 || venue_ts_ms <= 0 {
            return Err(VenueError::BadReply(format!(
                "execution {exec_id} in {symbol} has non-positive quantity, price, or timestamp"
            )));
        }
        let fee = self.exec_fee.optional("execFee")?;
        let legacy_fee = fee
            .as_ref()
            .map(|fee| fee.value.to_f64())
            .transpose()
            .map_err(|e| VenueError::BadReply(format!("execFee: {e}")))?;
        let amounts = ExecutionAmounts {
            settlement_asset: AssetId::Unknown,
            quantity,
            price,
            fee: fee.map(|amount| AssetAmount {
                asset: match self.fee_currency.text() {
                    "" => AssetId::Unknown,
                    asset => AssetId::Named(asset.to_owned()),
                },
                amount,
            }),
        };
        Ok(Some(VenueExecution {
            exec_id: exec_id.to_owned(),
            client_order_id: if stream {
                self.order_link_id.required("orderLinkId")?
            } else {
                self.order_link_id.text()
            }
            .to_owned(),
            symbol: symbol.to_owned(),
            side,
            qty,
            px,
            fee: legacy_fee,
            amounts: Some(amounts),
            is_maker: self.is_maker.0.unwrap_or(false),
            forced_close: classify_close(
                self.create_type.text(),
                self.stop_order_type.text(),
                exec_type,
            ),
            venue_ts_ms,
        }))
    }
}

pub(crate) fn classify_close(create: &str, stop: &str, execution: &str) -> Option<ForcedClose> {
    let create = match create {
        "CreateByStopLoss" | "CreateByPartialStopLoss" | "CreateByTrailingStop" => {
            Some(ForcedClose::StopLoss)
        }
        "CreateByTakeProfit" | "CreateByPartialTakeProfit" | "CreateByTrailingProfit" => {
            Some(ForcedClose::TakeProfit)
        }
        "CreateByLiq" | "CreateByTakeOver_PassThrough" => Some(ForcedClose::Liquidation),
        "CreateByAdl_PassThrough" => Some(ForcedClose::AutoDeleverage),
        _ => None,
    };
    let stop = match stop {
        "StopLoss" | "PartialStopLoss" | "TrailingStop" => Some(ForcedClose::StopLoss),
        "TakeProfit" | "PartialTakeProfit" => Some(ForcedClose::TakeProfit),
        _ => None,
    };
    let execution = match execution {
        "BustTrade" => Some(ForcedClose::Liquidation),
        "AdlTrade" => Some(ForcedClose::AutoDeleverage),
        _ => None,
    };
    create.or(stop).or(execution)
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct HistoryReply {
    ret_code: i64,
    #[serde(default)]
    ret_msg: String,
    #[serde(default)]
    result: Option<Box<serde_json::value::RawValue>>,
}

impl HistoryReply {
    pub(crate) fn executions(self) -> Result<(Vec<VenueExecution>, String, [u8; 32]), VenueError> {
        if self.ret_code != 0 {
            return Err(VenueError::Rejected {
                code: self.ret_code,
                message: self.ret_msg,
            });
        }
        #[derive(Deserialize)]
        #[serde(rename_all = "camelCase")]
        struct Page {
            list: Vec<Box<serde_json::value::RawValue>>,
            next_page_cursor: String,
        }
        let result = self
            .result
            .ok_or_else(|| VenueError::BadReply("execution reply carries no result".into()))?;
        let page: Page = engine_public::numeric_wire::decode_object(result.get())
            .map_err(|e| VenueError::BadReply(e.to_string()))?;
        let mut executions = Vec::with_capacity(page.list.len());
        use sha2::Digest;
        let mut digest = sha2::Sha256::new();
        for raw in page.list {
            digest.update(raw.get().len().to_le_bytes());
            digest.update(raw.get().as_bytes());
            if let Some(mut execution) = ExecutionRow::decode_raw(raw.get())?.normalized(false)? {
                // This history request is explicitly scoped to settleCoin=USDT.
                let amounts = execution
                    .amounts
                    .as_mut()
                    .expect("normalized execution amounts");
                amounts.settlement_asset = AssetId::Named("USDT".to_owned());
                if let Some(fee) = &mut amounts.fee {
                    if fee.asset == AssetId::Unknown {
                        fee.asset = amounts.settlement_asset.clone();
                    }
                }
                executions.push(execution);
            }
        }
        Ok((executions, page.next_page_cursor, digest.finalize().into()))
    }
}

#[cfg(test)]
mod history_tests {
    use super::*;
    #[test]
    fn a_history_error_without_result_keeps_its_rejection_code() {
        let decoded =
            serde_json::from_str::<HistoryReply>(r#"{"retCode":10006,"retMsg":"rate limit"}"#);
        let reply = decoded.expect("business error envelope must decode without success payload");
        assert!(matches!(
            reply.executions(),
            Err(VenueError::Rejected { code: 10006, .. })
        ));
    }
}
