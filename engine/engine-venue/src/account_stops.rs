use std::collections::HashMap;

use engine_public::numeric_wire::{DecimalField, IntegerField};
use engine_types::numeric::Exact;
use engine_types::{PositionView, Side, VenueError};
use serde::Deserialize;
use serde_json::value::RawValue;

use crate::wire::{Field, Id};

#[derive(Default)]
pub(crate) struct Triggers {
    long: Option<Exact>,
    short: Option<Exact>,
}
impl Triggers {
    fn add(&mut self, price: Exact, side: Option<Side>) {
        if side != Some(Side::Sell) {
            self.long = Some(
                self.long
                    .take()
                    .map_or_else(|| price.clone(), |old| old.max(price.clone())),
            );
        }
        if side != Some(Side::Buy) {
            self.short = Some(
                self.short
                    .take()
                    .map_or_else(|| price.clone(), |old| old.min(price.clone())),
            );
        }
    }
    pub(crate) fn for_side(&self, side: Side) -> Option<&Exact> {
        match side {
            Side::Buy => self.long.as_ref(),
            Side::Sell => self.short.as_ref(),
        }
    }
}
#[derive(Default)]
pub(crate) struct OrderStops {
    orders: HashMap<u64, StopOrder>,
}
struct StopOrder {
    position_side: Side,
    quantity: Exact,
    trigger: Exact,
}
impl OrderStops {
    fn add(
        &mut self,
        id: u64,
        position_side: Side,
        quantity: Exact,
        trigger: Exact,
    ) -> Result<(), VenueError> {
        if self
            .orders
            .insert(
                id,
                StopOrder {
                    position_side,
                    quantity,
                    trigger,
                },
            )
            .is_some()
        {
            return Err(bad("native stop order id appears twice"));
        }
        Ok(())
    }
    pub(crate) fn covering(&self, side: Side, quantity: &Exact) -> Option<&Exact> {
        let mut orders: Vec<_> = self
            .orders
            .values()
            .filter(|order| order.position_side == side)
            .collect();
        orders.sort_by(|a, b| match side {
            Side::Buy => b.trigger.cmp(&a.trigger),
            Side::Sell => a.trigger.cmp(&b.trigger),
        });
        let mut covered = Exact::zero();
        for order in orders {
            covered += &order.quantity;
            if &covered >= quantity {
                return Some(&order.trigger);
            }
        }
        None
    }
}
fn bad(error: impl std::fmt::Display) -> VenueError {
    VenueError::BadReply(error.to_string())
}
fn decode<T: serde::de::DeserializeOwned>(raw: &str) -> Result<T, VenueError> {
    serde_json::from_str(raw).map_err(bad)
}
fn positive(field: &DecimalField, name: &str) -> Result<Option<Exact>, VenueError> {
    let Some(number) = field.optional(name)? else {
        return Ok(None);
    };
    if number.value.is_negative() {
        return Err(bad(format!("negative native {name}")));
    }
    if number.value.is_zero() {
        return Ok(None);
    }
    number.value.to_f64().map_err(bad)?;
    Ok(Some(number.value))
}
pub(crate) fn assign(position: &mut PositionView, price: Option<&Exact>) -> Result<(), VenueError> {
    position.stop_px = price
        .map(Exact::to_f64)
        .transpose()
        .map_err(bad)?
        .unwrap_or(0.0);
    position.stop_attached = price.is_some();
    position.exact_stop_px = price.cloned().map(Box::new);
    Ok(())
}

pub(crate) fn bybit(raw: &str) -> Result<HashMap<String, Triggers>, VenueError> {
    #[derive(Deserialize)]
    struct Reply {
        result: Rows,
    }
    #[derive(Deserialize)]
    struct Rows {
        list: Vec<Row>,
    }
    #[derive(Deserialize)]
    struct Row {
        symbol: String,
        #[serde(default)]
        side: Field<String>,
        #[serde(default, rename = "stopLoss")]
        stop: DecimalField,
    }
    let reply: Reply = decode(raw)?;
    let mut out = HashMap::<String, Triggers>::new();
    for row in reply.result.list {
        let Some(price) = positive(&row.stop, "stopLoss")? else {
            continue;
        };
        let side = match row.side.text() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            _ => return Err(bad("native stop has no position side")),
        };
        out.entry(row.symbol).or_default().add(price, Some(side));
    }
    Ok(out)
}

pub(crate) fn binance(raw: &str) -> Result<HashMap<String, Triggers>, VenueError> {
    #[derive(Deserialize)]
    struct Row {
        #[serde(default)]
        symbol: Field<String>,
        #[serde(default)]
        side: Field<String>,
        #[serde(default, rename = "algoType")]
        algo: Field<String>,
        #[serde(default, rename = "orderType")]
        kind: Field<String>,
        #[serde(default, rename = "workingType")]
        working: Field<String>,
        #[serde(default, rename = "closePosition")]
        close: Field<bool>,
        #[serde(default, rename = "triggerPrice")]
        trigger: DecimalField,
    }
    let rows: Vec<Row> = decode(raw)?;
    let mut out = HashMap::<String, Triggers>::new();
    for row in rows {
        if row.algo.text() != "CONDITIONAL"
            || row.kind.text() != "STOP_MARKET"
            || row.working.text() != "MARK_PRICE"
            || row.close.0 != Some(true)
        {
            continue;
        }
        let Some(price) = positive(&row.trigger, "triggerPrice")? else {
            continue;
        };
        let side = match row.side.text() {
            "SELL" => Side::Buy,
            "BUY" => Side::Sell,
            _ => return Err(bad("native stop has no order side")),
        };
        out.entry(row.symbol.required("symbol")?.into())
            .or_default()
            .add(price, Some(side));
    }
    Ok(out)
}

pub(crate) fn hyperliquid(raw: &str) -> Result<HashMap<String, OrderStops>, VenueError> {
    #[derive(Deserialize)]
    struct Row {
        #[serde(default)]
        coin: Field<String>,
        #[serde(default)]
        oid: Option<u64>,
        #[serde(default)]
        side: Field<String>,
        #[serde(default)]
        sz: DecimalField,
        #[serde(default, rename = "isTrigger")]
        trigger_order: Field<bool>,
        #[serde(default, rename = "reduceOnly")]
        reduce: Field<bool>,
        #[serde(default, rename = "orderType")]
        kind: Field<String>,
        #[serde(default, rename = "triggerPx")]
        trigger: DecimalField,
    }
    let rows: Vec<Row> = decode(raw)?;
    let mut out = HashMap::<String, OrderStops>::new();
    for row in rows {
        if row.trigger_order.0 != Some(true)
            || row.reduce.0 != Some(true)
            || !["Stop Market", "Stop Limit"]
                .iter()
                .any(|kind| row.kind.text().eq_ignore_ascii_case(kind))
        {
            continue;
        }
        let Some(price) = positive(&row.trigger, "triggerPx")? else {
            continue;
        };
        let Some(quantity) = positive(&row.sz, "stop remaining size")? else {
            continue;
        };
        let side = match row.side.text() {
            "A" => Side::Buy,
            "B" => Side::Sell,
            _ => continue,
        };
        let Some(id) = row.oid else {
            continue;
        };
        out.entry(row.coin.required("coin")?.into())
            .or_default()
            .add(id, side, quantity, price)?;
    }
    Ok(out)
}

pub(crate) fn lighter(raw: &str) -> Result<HashMap<i16, OrderStops>, VenueError> {
    #[derive(Deserialize)]
    struct Reply {
        orders: Vec<Row>,
    }
    #[derive(Deserialize)]
    struct Row {
        #[serde(default)]
        market_index: IntegerField,
        #[serde(default)]
        order_index: Option<u64>,
        #[serde(default)]
        is_ask: Field<bool>,
        #[serde(default)]
        remaining_base_amount: DecimalField,
        #[serde(default)]
        reduce_only: Field<bool>,
        #[serde(default, rename = "type")]
        kind: Field<String>,
        #[serde(default)]
        trigger_price: DecimalField,
    }
    let reply: Reply = decode(raw)?;
    let mut out = HashMap::<i16, OrderStops>::new();
    for row in reply.orders {
        if row.reduce_only.0 != Some(true)
            || ![
                "stop-loss",
                "stop_loss",
                "stop-loss-limit",
                "stop_loss_limit",
            ]
            .iter()
            .any(|kind| row.kind.text().eq_ignore_ascii_case(kind))
        {
            continue;
        }
        let Some(price) = positive(&row.trigger_price, "trigger_price")? else {
            continue;
        };
        let Some(quantity) = positive(&row.remaining_base_amount, "stop remaining amount")? else {
            continue;
        };
        let Some(is_ask) = row.is_ask.0 else {
            continue;
        };
        let Some(id) = row.order_index else {
            continue;
        };
        let index = i16::try_from(row.market_index.required("market_index")?).map_err(bad)?;
        out.entry(index).or_default().add(
            id,
            if is_ask { Side::Buy } else { Side::Sell },
            quantity,
            price,
        )?;
    }
    Ok(out)
}

pub(crate) fn mexc(raw: &str) -> Result<HashMap<String, Triggers>, VenueError> {
    #[derive(Deserialize)]
    struct Reply {
        data: Box<RawValue>,
    }
    #[derive(Deserialize)]
    struct Paged {
        #[serde(rename = "resultList")]
        rows: Vec<Row>,
    }
    #[derive(Deserialize)]
    struct Row {
        #[serde(default, rename = "positionId")]
        position: Option<Id>,
        #[serde(default, rename = "orderId")]
        order: Option<Id>,
        #[serde(default, rename = "stopLossPrice")]
        trigger: DecimalField,
    }
    let reply: Reply = decode(raw)?;
    let rows: Vec<Row> = if reply.data.get().starts_with('[') {
        decode(reply.data.get())?
    } else {
        decode::<Paged>(reply.data.get())?.rows
    };
    let mut out = HashMap::<String, Triggers>::new();
    for row in rows {
        let order = row.order.map(Id::into_text).unwrap_or_default();
        if !order.is_empty() && order != "0" {
            continue;
        }
        let Some(price) = positive(&row.trigger, "stopLossPrice")? else {
            continue;
        };
        let position = row
            .position
            .ok_or_else(|| bad("native stop has no position id"))?
            .into_text();
        out.entry(position).or_default().add(price, None);
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn native_stop_extremes_are_selected_before_binary64_rounding() {
        let low = "90.00000000000000000001";
        let high = "90.00000000000000000002";
        let raw = format!(
            r#"[
            {{"coin":"BTC","oid":1,"side":"A","sz":"1","isTrigger":true,"reduceOnly":true,"orderType":"Stop Market","triggerPx":{low}}},
            {{"coin":"BTC","oid":2,"side":"A","sz":"1","isTrigger":true,"reduceOnly":true,"orderType":"Stop Market","triggerPx":"{high}"}},
            {{"coin":"BTC","oid":3,"side":"B","sz":"1","isTrigger":true,"reduceOnly":true,"orderType":"Stop Market","triggerPx":{low}}},
            {{"coin":"BTC","oid":4,"side":"B","sz":"1","isTrigger":true,"reduceOnly":true,"orderType":"Stop Market","triggerPx":"{high}"}}
        ]"#
        );
        let stops = hyperliquid(&raw).unwrap();
        assert_eq!(
            stops["BTC"].covering(Side::Buy, &Exact::one()),
            Some(&Exact::parse_decimal(high).unwrap())
        );
        assert_eq!(
            stops["BTC"].covering(Side::Sell, &Exact::one()),
            Some(&Exact::parse_decimal(low).unwrap())
        );
        assert_eq!(
            stops["BTC"].covering(Side::Buy, &Exact::from_u64(2)),
            Some(&Exact::parse_decimal(low).unwrap())
        );
        assert_eq!(
            stops["BTC"].covering(Side::Sell, &Exact::from_u64(2)),
            Some(&Exact::parse_decimal(high).unwrap())
        );
    }

    #[test]
    fn absent_zero_invalid_and_out_of_range_native_stop_values_stay_distinct() {
        for value in ["null", "0", "\"\""] {
            let raw = format!(
                r#"{{"data":[{{"positionId":"7","orderId":"0","stopLossPrice":{value}}}]}}"#
            );
            assert!(mexc(&raw).unwrap().is_empty());
        }
        for value in ["-1", "1e-400", "1e5000", "\"bad\"", "{}"] {
            let raw = format!(
                r#"{{"data":[{{"positionId":"7","orderId":"0","stopLossPrice":{value}}}]}}"#
            );
            assert!(mexc(&raw).is_err(), "{value}");
        }
        let bound_to_order = r#"{"data":[{"positionId":"7","orderId":"9","stopLossPrice":"bad"}]}"#;
        assert!(mexc(bound_to_order).unwrap().is_empty());
        assert!(binance(
            r#"[{"symbol":"BTCUSDT","orderType":"TAKE_PROFIT_MARKET","triggerPrice":"bad"}]"#
        )
        .unwrap()
        .is_empty());
    }

    #[test]
    fn binance_native_stops_remain_bound_to_the_side_they_close() {
        let raw = r#"[{"symbol":"BTCUSDT","algoType":"CONDITIONAL","orderType":"STOP_MARKET","closePosition":true,"workingType":"MARK_PRICE","side":"SELL","triggerPrice":90}]"#;
        let stops = binance(raw).unwrap();
        assert_eq!(
            stops["BTCUSDT"].for_side(Side::Buy),
            Some(&Exact::from_u64(90))
        );
        assert!(stops["BTCUSDT"].for_side(Side::Sell).is_none());
    }
}
