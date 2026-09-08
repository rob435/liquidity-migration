use engine_public::numeric_wire::DecimalField;
#[cfg(any(test, feature = "binance", feature = "hyperliquid"))]
use engine_types::numeric::Exact;
#[cfg(any(test, feature = "binance"))]
use engine_types::numeric::ExactNumber;
use engine_types::order_terms::ExactAmendedTerms;
use engine_types::{OrderUpdate, VenueError};
use serde::Deserialize;

fn bad(error: impl std::fmt::Display) -> VenueError {
    VenueError::BadReply(error.to_string())
}
fn news(
    terms: ExactAmendedTerms,
    id: &str,
    recv_ns: u64,
    allow_zero: bool,
) -> Result<OrderUpdate, VenueError> {
    let px = terms.price.value.to_f64().map_err(bad)?;
    let qty = terms.quantity.value.to_f64().map_err(bad)?;
    terms.validate_projection(px, qty).map_err(bad)?;
    if !allow_zero && qty == 0.0 {
        return Err(bad("no remaining order quantity"));
    }
    Ok(OrderUpdate::Amended {
        exact_terms: Some(Box::new(terms)),
        client_order_id: id.to_owned(),
        px,
        qty,
        recv_ns,
    })
}

#[cfg(any(test, feature = "bybit"))]
pub(crate) fn bybit(raw: &str, id: &str, recv_ns: u64) -> Result<OrderUpdate, VenueError> {
    #[derive(Default, Deserialize)]
    #[serde(default)]
    struct Row {
        price: DecimalField,
        #[serde(rename = "leavesQty")]
        leaves: DecimalField,
        qty: DecimalField,
    }
    let row: Row = crate::wire::raw_object(raw).map_err(bad)?;
    let quantity = match row.leaves.optional("leavesQty")? {
        Some(qty) => qty,
        None => row.qty.required("qty")?,
    };
    news(
        ExactAmendedTerms {
            price: row.price.required("price")?,
            quantity,
        },
        id,
        recv_ns,
        false,
    )
}

#[cfg(any(test, feature = "hyperliquid"))]
pub(crate) fn hyperliquid(raw: &str, id: &str, recv_ns: u64) -> Result<OrderUpdate, VenueError> {
    #[derive(Default, Deserialize)]
    #[serde(default)]
    struct Row {
        #[serde(rename = "limitPx")]
        price: DecimalField,
        sz: DecimalField,
    }
    let row: Row = crate::wire::raw_object(raw).map_err(bad)?;
    news(
        ExactAmendedTerms {
            price: row.price.required("limitPx")?,
            quantity: row.sz.required("sz")?,
        },
        id,
        recv_ns,
        false,
    )
}

#[cfg(any(test, feature = "binance"))]
pub(crate) fn binance(raw: &str, id: &str, recv_ns: u64) -> Result<OrderUpdate, VenueError> {
    #[derive(Default, Deserialize)]
    #[serde(default)]
    struct Row {
        p: DecimalField,
        q: DecimalField,
        z: DecimalField,
    }
    let row: Row = crate::wire::raw_object(raw).map_err(bad)?;
    let total = row.q.required("q")?.value;
    let filled = row.z.required("z")?.value;
    if total.is_negative() || filled.is_negative() || filled > total {
        return Err(bad("amended total and filled quantities are inconsistent"));
    }
    let remaining: Exact = total - filled;
    news(
        ExactAmendedTerms {
            price: row.p.required("p")?,
            quantity: ExactNumber::derived(remaining),
        },
        id,
        recv_ns,
        true,
    )
}

#[cfg(feature = "binance")]
pub(crate) fn resting_binance(
    raw: &str,
    symbol: &str,
    id: &str,
) -> Result<(Exact, Exact), VenueError> {
    #[derive(Deserialize)]
    struct Row {
        symbol: String,
        #[serde(rename = "clientOrderId")]
        id: String,
        price: DecimalField,
        #[serde(rename = "origQty")]
        qty: DecimalField,
    }
    let row: Row = serde_json::from_str(raw).map_err(bad)?;
    if row.symbol != symbol || row.id != id {
        return Err(bad("amend lookup returned another order"));
    }
    Ok((
        row.price.required("price")?.value,
        row.qty.required("origQty")?.value,
    ))
}

#[cfg(feature = "hyperliquid")]
pub(crate) fn resting_hyperliquid(
    raw: &str,
    coin: &str,
    cloid: &str,
) -> Result<(Exact, Exact), VenueError> {
    #[derive(Deserialize)]
    struct Row {
        coin: String,
        cloid: String,
        #[serde(rename = "limitPx")]
        price: DecimalField,
        sz: DecimalField,
    }
    let row: Row = serde_json::from_str(raw).map_err(bad)?;
    if row.coin != coin || row.cloid != cloid {
        return Err(bad("amend lookup returned another order"));
    }
    Ok((
        row.price.required("limitPx")?.value,
        row.sz.required("sz")?.value,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    fn check(news: OrderUpdate, qty: &str) {
        let OrderUpdate::Amended {
            exact_terms: Some(terms),
            px,
            qty: scalar,
            ..
        } = news
        else {
            panic!("missing exact news")
        };
        assert_eq!(
            terms.price.value,
            Exact::parse_decimal("100.123456789012345678901").unwrap()
        );
        assert_eq!(terms.quantity.value, Exact::parse_decimal(qty).unwrap());
        terms.validate_projection(px, scalar).unwrap();
    }
    #[test]
    fn all_amending_private_protocols_preserve_exact_price_and_remaining_size() {
        check(bybit(r#"{"price":100.123456789012345678901,"leavesQty":"0.123456789012345678901","qty":"2"}"#,"id",1).unwrap(),"0.123456789012345678901");
        check(
            hyperliquid(
                r#"{"limitPx":"100.123456789012345678901","sz":0.123456789012345678901}"#,
                "id",
                1,
            )
            .unwrap(),
            "0.123456789012345678901",
        );
        check(
            binance(
                r#"{"p":"100.123456789012345678901","q":"0.223456789012345678901","z":"0.1"}"#,
                "id",
                1,
            )
            .unwrap(),
            "0.123456789012345678901",
        );
    }
    #[test]
    fn incomplete_malformed_negative_and_extreme_amend_fields_stay_unresolved() {
        for raw in [
            r#"{"price":"10","leavesQty":"broken","qty":"2"}"#,
            r#"{"price":"10","leavesQty":"0","qty":"2"}"#,
            r#"{"price":"1e-99999","leavesQty":"1"}"#,
            r#"{"price":"10","qty":"-1"}"#,
        ] {
            assert!(bybit(raw, "id", 1).is_err(), "{raw}");
        }
        for raw in [
            r#"{"limitPx":"10"}"#,
            r#"{"limitPx":"10","sz":"0"}"#,
            r#"{"limitPx":"NaN","sz":"1"}"#,
        ] {
            assert!(hyperliquid(raw, "id", 1).is_err(), "{raw}");
        }
        for raw in [
            r#"{"p":"10","q":"1"}"#,
            r#"{"p":"10","q":"1","z":"2"}"#,
            r#"{"p":"10","q":"1","z":"-1"}"#,
        ] {
            assert!(binance(raw, "id", 1).is_err(), "{raw}");
        }
        assert!(matches!(
            binance(r#"{"p":"10","q":"1","z":"1"}"#, "id", 1).unwrap(),
            OrderUpdate::Amended { qty: 0.0, .. }
        ));
    }
}
