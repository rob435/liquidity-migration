use crate::wire::{Field, Id};
use engine_public::numeric_wire::{DecimalField, IntegerField};
use engine_types::numeric::Exact;
use engine_types::{Side, VenueError};
use serde::Deserialize;

fn bad(detail: impl Into<String>) -> VenueError {
    VenueError::BadReply(detail.into())
}
fn decode<T: serde::de::DeserializeOwned>(raw: &str) -> Result<T, VenueError> {
    serde_json::from_str(raw).map_err(|e| bad(e.to_string()))
}
fn one(rows: impl Iterator<Item = (Side, Exact)>) -> Result<(Side, Exact), VenueError> {
    let mut rows = rows.filter(|(_, qty)| !qty.is_zero());
    let first = rows
        .next()
        .ok_or_else(|| VenueError::BadRequest("no nonzero native position for stop".into()))?;
    if rows.next().is_some() {
        return Err(bad("multiple native position rows for one stop"));
    }
    Ok(first)
}
fn signed(value: Exact) -> (Side, Exact) {
    (
        if value.is_negative() {
            Side::Sell
        } else {
            Side::Buy
        },
        value.abs(),
    )
}

pub(crate) fn hyperliquid(raw: &str, coin: &str) -> Result<(Side, Exact), VenueError> {
    #[derive(Deserialize)]
    struct State {
        #[serde(rename = "assetPositions")]
        rows: Vec<Outer>,
    }
    #[derive(Deserialize)]
    struct Outer {
        position: Position,
    }
    #[derive(Deserialize)]
    struct Position {
        coin: String,
        szi: DecimalField,
    }
    let state: State = decode(raw)?;
    let rows = state
        .rows
        .into_iter()
        .filter(|row| row.position.coin == coin)
        .map(|row| {
            row.position
                .szi
                .required("szi")
                .map(|amount| signed(amount.value))
        })
        .collect::<Result<Vec<_>, _>>()?;
    one(rows.into_iter())
}

pub(crate) fn lighter(
    raw: &str,
    index: i16,
    symbol: &str,
    account_index: i64,
) -> Result<(Side, Exact), VenueError> {
    #[derive(Deserialize)]
    struct Reply {
        code: i64,
        #[serde(default)]
        message: Field<String>,
        #[serde(default)]
        accounts: Option<Vec<Account>>,
    }
    #[derive(Deserialize)]
    struct Account {
        #[serde(default)]
        account_index: IntegerField,
        positions: Vec<Position>,
    }
    #[derive(Deserialize)]
    struct Position {
        market_id: IntegerField,
        symbol: String,
        sign: IntegerField,
        position: DecimalField,
    }
    let reply: Reply = decode(raw)?;
    if reply.code != 200 {
        return Err(VenueError::Rejected {
            code: reply.code,
            message: reply.message.text().into(),
        });
    }
    let mut accounts = reply
        .accounts
        .ok_or_else(|| bad("account reply has no accounts"))?
        .into_iter();
    let account = accounts
        .next()
        .ok_or_else(|| bad("account reply is empty"))?;
    if accounts.next().is_some() {
        return Err(bad("account lookup returned multiple accounts"));
    }
    if matches!(account.account_index, IntegerField::Number(value) if value != account_index) {
        return Err(bad("account lookup returned another account"));
    }
    let mut out = Vec::new();
    for row in account.positions {
        let market = row.market_id.required("market_id")?;
        if market != i64::from(index) {
            continue;
        }
        if row.symbol != symbol {
            return Err(bad("position market index names another symbol"));
        }
        let amount = row.position.required("position")?.value;
        if amount.is_negative() {
            return Err(bad("position quantity is negative"));
        }
        if amount.is_zero() {
            continue;
        }
        let side = match row.sign.required("sign")? {
            1 => Side::Buy,
            -1 => Side::Sell,
            _ => return Err(bad("position sign is unknown")),
        };
        out.push((side, amount));
    }
    one(out.into_iter())
}

pub(crate) fn bybit(raw: &str, symbol: &str) -> Result<(Side, Exact), VenueError> {
    #[derive(Deserialize)]
    struct Reply {
        #[serde(rename = "retCode")]
        code: i64,
        #[serde(default, rename = "retMsg")]
        message: Field<String>,
        #[serde(default)]
        result: Option<ResultRows>,
    }
    #[derive(Deserialize)]
    struct ResultRows {
        list: Vec<Position>,
    }
    #[derive(Deserialize)]
    struct Position {
        symbol: String,
        #[serde(rename = "positionIdx")]
        index: IntegerField,
        side: String,
        size: DecimalField,
    }
    let reply: Reply = decode(raw)?;
    if reply.code != 0 {
        return Err(VenueError::Rejected {
            code: reply.code,
            message: reply.message.text().into(),
        });
    }
    let mut out = Vec::new();
    for row in reply
        .result
        .ok_or_else(|| bad("position reply has no result"))?
        .list
    {
        if row.symbol != symbol {
            continue;
        }
        if row.index.required("positionIdx")? != 0 {
            return Err(bad("stop requires one-way position mode"));
        }
        let size = row.size.required("size")?.value;
        if size.is_negative() {
            return Err(bad("position size is negative"));
        }
        if size.is_zero() {
            continue;
        }
        let side = match row.side.as_str() {
            "Buy" => Side::Buy,
            "Sell" => Side::Sell,
            _ => return Err(bad("position side is unknown")),
        };
        out.push((side, size));
    }
    one(out.into_iter())
}

pub(crate) fn mexc(raw: &str, symbol: &str) -> Result<(String, Side), VenueError> {
    #[derive(Deserialize)]
    struct Reply {
        success: bool,
        code: i64,
        #[serde(default)]
        message: Field<String>,
        #[serde(default)]
        data: Option<Vec<Position>>,
    }
    #[derive(Deserialize)]
    struct Position {
        symbol: String,
        #[serde(rename = "positionId")]
        id: Id,
        #[serde(rename = "positionType")]
        kind: IntegerField,
        #[serde(rename = "holdVol")]
        volume: DecimalField,
    }
    let reply: Reply = decode(raw)?;
    if !reply.success || reply.code != 0 {
        return Err(VenueError::Rejected {
            code: reply.code,
            message: reply.message.text().into(),
        });
    }
    let mut out = Vec::new();
    for row in reply
        .data
        .ok_or_else(|| bad("position reply has no data"))?
    {
        if row.symbol != symbol {
            continue;
        }
        let volume = row.volume.required("holdVol")?.value;
        if volume.is_negative() {
            return Err(bad("position volume is negative"));
        }
        if volume.is_zero() {
            continue;
        }
        let side = match row.kind.required("positionType")? {
            1 => Side::Buy,
            2 => Side::Sell,
            _ => return Err(bad("position type is unknown")),
        };
        out.push((row.id.into_text(), side));
    }
    if out.len() != 1 {
        return Err(bad("expected one native position for stop"));
    }
    Ok(out.remove(0))
}

pub(crate) fn binance(raw: &str, symbol: &str) -> Result<(Side, Exact), VenueError> {
    #[derive(Deserialize)]
    struct Account {
        positions: Vec<Position>,
    }
    #[derive(Deserialize)]
    struct Position {
        symbol: String,
        #[serde(rename = "positionSide")]
        side: String,
        #[serde(rename = "positionAmt")]
        amount: DecimalField,
    }
    let account: Account = decode(raw)?;
    let mut out = Vec::new();
    for row in account.positions {
        if row.symbol != symbol {
            continue;
        }
        if row.side != "BOTH" {
            return Err(bad("stop requires one-way position mode"));
        }
        out.push(signed(row.amount.required("positionAmt")?.value));
    }
    one(out.into_iter())
}
