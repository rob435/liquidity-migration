//! Native account decimals are decoded before compatibility JSON projections.
use engine_public::numeric_wire::{decode_object, DecimalField};
use engine_types::numeric::ExactNumber;
use engine_types::risk::{AccountAmounts, PositionAmounts};
use engine_types::{PositionView, Side, Symbol, VenueError};
use serde::Deserialize;
#[cfg(feature = "mexc")]
use serde_json::value::RawValue;
use std::collections::BTreeMap;
fn bad(e: impl std::fmt::Display) -> VenueError {
    VenueError::BadReply(e.to_string())
}
fn decode<T: serde::de::DeserializeOwned>(raw: &str) -> Result<T, VenueError> {
    decode_object(raw).map_err(bad)
}
fn money(equity: DecimalField, available: DecimalField) -> Result<AccountAmounts, VenueError> {
    Ok(AccountAmounts {
        equity_usdt: equity.required("account equity")?,
        available_usdt: available.required("available margin")?,
    })
}
#[derive(Debug)]
pub(crate) struct NativePosition {
    symbol: String,
    side: Side,
    amounts: PositionAmounts,
}
fn held(
    symbol: String,
    side: Side,
    qty: ExactNumber,
    entry: DecimalField,
) -> Result<NativePosition, VenueError> {
    let quantity = if qty.value.is_negative() {
        ExactNumber::derived(qty.value.abs())
    } else {
        qty
    };
    Ok(NativePosition {
        symbol,
        side,
        amounts: PositionAmounts {
            quantity,
            entry_price: entry.required("entry price")?,
        },
    })
}
pub(crate) fn assign(
    positions: &mut [PositionView],
    native: Vec<NativePosition>,
    symbols: &[Symbol],
) -> Result<(), VenueError> {
    let mut rows = BTreeMap::new();
    for row in native {
        if rows
            .insert((row.symbol, row.side == Side::Sell), row.amounts)
            .is_some()
        {
            return Err(bad("native account repeats a position side"));
        }
    }
    for position in positions {
        let symbol = symbols
            .get(position.symbol.idx())
            .ok_or_else(|| bad("position has unknown symbol id"))?;
        let amounts = rows
            .remove(&(symbol.clone(), position.side == Side::Sell))
            .ok_or_else(|| bad("canonical account position join is incomplete"))?;
        position.qty = amounts.quantity.value.to_f64().map_err(bad)?;
        position.entry_px = amounts.entry_price.value.to_f64().map_err(bad)?;
        position.exact_amounts = Some(Box::new(amounts));
    }
    if !rows.is_empty() {
        return Err(bad(
            "canonical account contains an unreported nonzero position",
        ));
    }
    Ok(())
}
#[cfg(feature = "bybit")]
#[derive(Deserialize)]
struct BybitWallet {
    result: BybitWalletRows,
}
#[cfg(feature = "bybit")]
#[derive(Deserialize)]
struct BybitWalletRows {
    list: Vec<BybitBalance>,
}
#[cfg(feature = "bybit")]
#[derive(Deserialize)]
struct BybitBalance {
    #[serde(rename = "totalEquity")]
    equity: DecimalField,
    #[serde(rename = "totalAvailableBalance")]
    available: DecimalField,
}
#[cfg(feature = "bybit")]
pub(crate) fn bybit_wallet(raw: &str) -> Result<AccountAmounts, VenueError> {
    let reply: BybitWallet = decode(raw)?;
    let row = reply
        .result
        .list
        .into_iter()
        .next()
        .ok_or_else(|| bad("wallet has no account"))?;
    money(row.equity, row.available)
}
#[cfg(feature = "bybit")]
#[derive(Deserialize)]
struct BybitPositions {
    result: BybitRows,
}
#[cfg(feature = "bybit")]
#[derive(Deserialize)]
struct BybitRows {
    list: Vec<BybitPosition>,
}
#[cfg(feature = "bybit")]
#[derive(Deserialize)]
struct BybitPosition {
    symbol: String,
    #[serde(default)]
    side: String,
    size: DecimalField,
    #[serde(default, rename = "avgPrice")]
    entry: DecimalField,
}
#[cfg(feature = "bybit")]
pub(crate) fn bybit_positions(raw: &str) -> Result<Vec<NativePosition>, VenueError> {
    let reply: BybitPositions = decode(raw)?;
    let mut out = Vec::new();
    for row in reply.result.list {
        let qty = row.size.required("size")?;
        if qty.value.is_zero() {
            continue;
        }
        if qty.value.is_negative() {
            return Err(bad("negative unsigned position size"));
        }
        let side = match row.side.as_str() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            _ => return Err(bad("position has no side")),
        };
        out.push(held(row.symbol, side, qty, row.entry)?);
    }
    Ok(out)
}
#[cfg(feature = "binance")]
#[derive(Deserialize)]
struct BinanceAccount {
    #[serde(rename = "totalMarginBalance")]
    equity: DecimalField,
    #[serde(rename = "availableBalance")]
    available: DecimalField,
    positions: Vec<BinancePosition>,
}
#[cfg(feature = "binance")]
#[derive(Deserialize)]
struct BinancePosition {
    #[serde(default)]
    symbol: String,
    #[serde(rename = "positionAmt")]
    quantity: DecimalField,
    #[serde(default, rename = "entryPrice")]
    entry: DecimalField,
}
#[cfg(feature = "binance")]
pub(crate) fn binance(raw: &str) -> Result<(AccountAmounts, Vec<NativePosition>), VenueError> {
    let reply: BinanceAccount = decode(raw)?;
    let mut out = Vec::new();
    for row in reply.positions {
        let qty = row.quantity.required("positionAmt")?;
        if qty.value.is_zero() {
            continue;
        }
        let side = if qty.value.is_negative() {
            Side::Sell
        } else {
            Side::Buy
        };
        out.push(held(row.symbol, side, qty, row.entry)?);
    }
    Ok((money(reply.equity, reply.available)?, out))
}
#[cfg(feature = "hyperliquid")]
#[derive(Deserialize)]
struct HyperAccount {
    #[serde(rename = "marginSummary")]
    margin: HyperMargin,
    withdrawable: DecimalField,
    #[serde(rename = "assetPositions")]
    positions: Vec<HyperPositionWrapper>,
}
#[cfg(feature = "hyperliquid")]
#[derive(Deserialize)]
struct HyperMargin {
    #[serde(rename = "accountValue")]
    equity: DecimalField,
}
#[cfg(feature = "hyperliquid")]
#[derive(Deserialize)]
struct HyperPositionWrapper {
    position: HyperPosition,
}
#[cfg(feature = "hyperliquid")]
#[derive(Deserialize)]
struct HyperPosition {
    coin: String,
    szi: DecimalField,
    #[serde(default, rename = "entryPx")]
    entry: DecimalField,
}
#[cfg(feature = "hyperliquid")]
pub(crate) fn hyperliquid(raw: &str) -> Result<(AccountAmounts, Vec<NativePosition>), VenueError> {
    let reply: HyperAccount = decode(raw)?;
    let mut out = Vec::new();
    for wrapped in reply.positions {
        let row = wrapped.position;
        let qty = row.szi.required("szi")?;
        if qty.value.is_zero() {
            continue;
        }
        let side = if qty.value.is_negative() {
            Side::Sell
        } else {
            Side::Buy
        };
        out.push(held(
            crate::venues::hyperliquid::symbol_of(&row.coin),
            side,
            qty,
            row.entry,
        )?);
    }
    Ok((money(reply.margin.equity, reply.withdrawable)?, out))
}
#[cfg(feature = "lighter")]
#[derive(Deserialize)]
struct LighterAccount {
    accounts: Vec<LighterBalance>,
}
#[cfg(feature = "lighter")]
#[derive(Deserialize)]
struct LighterBalance {
    collateral: DecimalField,
    available_balance: DecimalField,
    positions: Vec<LighterPosition>,
}
#[cfg(feature = "lighter")]
#[derive(Deserialize)]
struct LighterPosition {
    symbol: String,
    position: DecimalField,
    #[serde(default)]
    sign: i64,
    #[serde(default)]
    avg_entry_price: DecimalField,
}
#[cfg(feature = "lighter")]
pub(crate) fn lighter(raw: &str) -> Result<(AccountAmounts, Vec<NativePosition>), VenueError> {
    let reply: LighterAccount = decode(raw)?;
    let row = reply
        .accounts
        .into_iter()
        .next()
        .ok_or_else(|| bad("account reply carries no account"))?;
    let mut out = Vec::new();
    for row in row.positions {
        let qty = row.position.required("position")?;
        if qty.value.is_zero() {
            continue;
        }
        if qty.value.is_negative() {
            return Err(bad("negative unsigned Lighter position quantity"));
        }
        let side = match row.sign {
            1 => Side::Buy,
            -1 => Side::Sell,
            _ => return Err(bad("Lighter position direction must be 1 or -1")),
        };
        out.push(held(
            engine_public::venues::lighter::markets::engine_symbol(&row.symbol),
            side,
            qty,
            row.avg_entry_price,
        )?);
    }
    Ok((money(row.collateral, row.available_balance)?, out))
}
#[cfg(feature = "mexc")]
#[derive(Deserialize)]
struct MexcAssets {
    data: Box<RawValue>,
}
#[cfg(feature = "mexc")]
#[derive(Deserialize)]
struct MexcBalance {
    #[serde(default)]
    currency: String,
    #[serde(default)]
    equity: DecimalField,
    #[serde(default, rename = "availableBalance")]
    available: DecimalField,
}
#[cfg(feature = "mexc")]
pub(crate) fn mexc_assets(raw: &str) -> Result<AccountAmounts, VenueError> {
    let reply: MexcAssets = decode(raw)?;
    let rows: Vec<MexcBalance> = serde_json::from_str(reply.data.get()).map_err(bad)?;
    let row = rows
        .into_iter()
        .find(|row| row.currency == "USDT")
        .ok_or_else(|| bad("no USDT account balance"))?;
    money(row.equity, row.available)
}
#[cfg(feature = "mexc")]
#[derive(Deserialize)]
struct MexcPositions {
    data: Vec<MexcPosition>,
}
#[cfg(feature = "mexc")]
#[derive(Deserialize)]
struct MexcPosition {
    #[serde(default)]
    symbol: String,
    #[serde(rename = "holdVol")]
    quantity: DecimalField,
    #[serde(default, rename = "holdAvgPrice")]
    entry: DecimalField,
    #[serde(default, rename = "positionType")]
    side: i64,
}
#[cfg(feature = "mexc")]
pub(crate) fn mexc_positions(
    raw: &str,
    contracts: &engine_public::venues::mexc::contracts::Contracts,
) -> Result<Vec<NativePosition>, VenueError> {
    let reply: MexcPositions = decode(raw)?;
    let mut out = Vec::new();
    for row in reply.data {
        let qty = row.quantity.required("holdVol")?;
        if qty.value.is_zero() {
            continue;
        }
        if qty.value.is_negative() {
            return Err(bad("negative contract quantity"));
        }
        let symbol = contracts
            .symbol_of(&row.symbol)
            .ok_or_else(|| bad("unmapped account contract"))?;
        let contract = contracts
            .any(symbol)
            .ok_or_else(|| bad("missing account contract metadata"))?;
        let qty = ExactNumber::derived(qty.value * &contract.exact_contract_size.value);
        let side = match row.side {
            1 => Side::Buy,
            2 => Side::Sell,
            _ => return Err(bad("invalid position type")),
        };
        out.push(held(symbol.clone(), side, qty, row.entry)?);
    }
    Ok(out)
}
#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(feature = "binance")]
    use engine_types::numeric::Exact;
    #[cfg(feature = "binance")]
    #[test]
    fn account_decimals_are_not_rounded_before_risk_sees_them() {
        let raw = r#"{"totalMarginBalance":9007199254740993.0000000000000000001,"availableBalance":"0.9999999999999999999","positions":[{"symbol":"BTCUSDT","positionAmt":9007199254740993.0000000000000000001,"entryPrice":"1.0000000000000000001"}]}"#;
        let (balance, positions) = binance(raw).unwrap();
        let quantity = Exact::parse_decimal("9007199254740993.0000000000000000001").unwrap();
        assert_eq!(balance.equity_usdt.value, quantity);
        assert_eq!(positions[0].amounts.quantity.value, quantity);
        assert_eq!(
            balance.available_usdt.value,
            Exact::parse_decimal("0.9999999999999999999").unwrap()
        );
        assert_ne!(positions[0].amounts.entry_price.value, Exact::one());
    }
    #[cfg(feature = "bybit")]
    #[test]
    fn unprojectable_position_is_never_silently_flat() {
        let raw =
            r#"{"result":{"list":[{"symbol":"BTCUSDT","side":"Buy","size":1e-400,"avgPrice":1}]}}"#;
        let rows = bybit_positions(raw).unwrap();
        assert!(assign(&mut [], rows, &["BTCUSDT".into()]).is_err());
    }
    #[cfg(feature = "bybit")]
    #[test]
    fn missing_invalid_and_negative_account_decimals_stay_distinct() {
        for amount in ["null", "\"NaN\"", "{}", "1e5000"] {
            let raw = format!(
                r#"{{"result":{{"list":[{{"totalEquity":{amount},"totalAvailableBalance":1}}]}}}}"#
            );
            assert!(bybit_wallet(&raw).is_err());
        }
        let raw = r#"{"result":{"list":[{"totalEquity":1,"totalAvailableBalance":-1}]}}"#;
        assert!(bybit_wallet(raw)
            .unwrap()
            .available_usdt
            .value
            .is_negative());
    }
}
