use super::markets::{engine_symbol, Markets};
use crate::wire::{Field, IntegerField, RawField};
use engine_public::numeric_wire::DecimalField;
use engine_types::numeric::{AssetAmount, AssetId, ExecutionAmounts};
use engine_types::{Side, VenueError, VenueExecution};
use serde::Deserialize;

#[derive(Default, Deserialize)]
#[serde(default)]
struct TradeRow {
    market_id: IntegerField,
    ask_account_id: IntegerField,
    bid_account_id: IntegerField,
    is_maker_ask: Field<bool>,
    ask_client_order_index: Field<i64>,
    bid_client_order_index: Field<i64>,
    trade_id: IntegerField,
    size: DecimalField,
    price: DecimalField,
    fee: DecimalField,
    fee_asset: Field<String>,
    timestamp: IntegerField,
}

#[derive(Default, Deserialize)]
#[serde(default)]
pub(crate) struct HistoryReply {
    code: Field<i64>,
    message: Field<String>,
    trades: RawField<Vec<Box<serde_json::value::RawValue>>>,
}

impl HistoryReply {
    pub(crate) fn executions(
        self,
        account: i64,
        markets: &Markets,
    ) -> Result<(Vec<VenueExecution>, usize), VenueError> {
        let code = self
            .code
            .0
            .ok_or_else(|| VenueError::BadReply("reply carries no code".to_owned()))?;
        if code != 200 {
            return Err(VenueError::Rejected {
                code,
                message: self.message.0.unwrap_or_else(|| "no message".to_owned()),
            });
        }
        let rows = self
            .trades
            .0
            .ok_or_else(|| VenueError::BadReply("the trade reply carries no trades".to_owned()))?;
        let count = rows.len();
        let mut executions = Vec::with_capacity(count);
        for raw in rows {
            let row: TradeRow = crate::wire::raw_object(raw.get())
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            let market = i16::try_from(row.market_id.required("market_id")?)
                .map_err(|_| VenueError::BadReply("a market index out of range".to_owned()))?;
            let symbol = markets
                .by_index(market)
                .map(|m| engine_symbol(&m.symbol))
                .unwrap_or_else(|| format!("market-{market}"));
            let ask = row.ask_account_id.required("ask_account_id")?;
            let bid = row.bid_account_id.required("bid_account_id")?;
            let sold = if ask == account {
                true
            } else if bid == account {
                false
            } else {
                continue;
            };
            let maker = row
                .is_maker_ask
                .0
                .map(|maker_ask| maker_ask == sold)
                .unwrap_or(false);
            let client_index = if sold {
                row.ask_client_order_index.0
            } else {
                row.bid_client_order_index.0
            }
            .unwrap_or(0);
            let fee = if maker {
                None
            } else {
                row.fee.optional("fee")?
            };
            let legacy_fee = fee
                .as_ref()
                .map(|fee| fee.value.to_f64())
                .transpose()
                .map_err(|e| VenueError::BadReply(e.to_string()))?;
            let amounts = ExecutionAmounts {
                settlement_asset: AssetId::Unknown,
                quantity: row.size.required("size")?,
                price: row.price.required("price")?,
                fee: fee.map(|amount| AssetAmount {
                    asset: match row.fee_asset.text() {
                        "" => AssetId::Unknown,
                        asset => AssetId::Named(asset.to_owned()),
                    },
                    amount,
                }),
            };
            executions.push(VenueExecution {
                exec_id: row.trade_id.required("trade_id")?.to_string(),
                client_order_id: super::order_index::from_index(client_index).unwrap_or_default(),
                symbol,
                side: if sold { Side::Sell } else { Side::Buy },
                qty: row.size.legacy("size")?,
                px: row.price.legacy("price")?,
                fee: legacy_fee,
                amounts: Some(amounts),
                is_maker: maker,
                forced_close: None,
                venue_ts_ms: row.timestamp.required("timestamp")?,
            });
        }
        Ok((executions, count))
    }
}
