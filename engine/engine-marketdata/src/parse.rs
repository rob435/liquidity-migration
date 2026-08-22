//! Raw Bybit frame to typed values. No sockets, no state, no allocation:
//! every function here is pure over the received bytes, which is what makes
//! the whole parse path testable without a network.

use std::fmt;

use engine_types::FeedError;
use serde::de::{self, Deserializer, SeqAccess, Visitor};
use serde::Deserialize;

/// One book level, already numeric.
#[derive(Copy, Clone, Debug, Default, PartialEq)]
pub struct Level {
    pub px: f64,
    pub qty: f64,
}

/// The levels one side of a book message carried. The engine subscribes at
/// depth 1 and consumes only the best level, so only that one is kept; `len`
/// still counts everything the message held, which is how a deeper push than
/// expected stays visible.
///
/// An absent side is `Option::None` at the frame level; an empty `Levels`
/// means the venue sent `[]`, which says "this side did not change".
#[derive(Copy, Clone, Debug, Default, PartialEq)]
pub struct Levels {
    best: Option<Level>,
    len: u16,
}

impl Levels {
    pub fn len(&self) -> usize {
        self.len as usize
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }

    /// The side's best level: index 0, which is where Bybit puts it on both
    /// sides (bids descending, asks ascending).
    pub fn best(&self) -> Option<Level> {
        self.best
    }
}

/// A book message, still keyed by venue symbol name.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct BookFrame<'a> {
    pub symbol: &'a str,
    /// True for `type: "snapshot"`, and for the `u == 1` service restart.
    pub snapshot: bool,
    pub update_id: u64,
    /// Absent means the message did not mention this side.
    pub bids: Option<Levels>,
    pub asks: Option<Levels>,
    pub venue_ts_ms: i64,
}

/// A ticker message. Every field is optional because a delta carries only
/// what changed.
#[derive(Copy, Clone, Debug, PartialEq)]
pub struct TickerFrame<'a> {
    pub symbol: &'a str,
    pub snapshot: bool,
    pub last_px: Option<f64>,
    pub mark_px: Option<f64>,
    pub index_px: Option<f64>,
    pub funding_rate: Option<f64>,
    pub next_funding_ms: Option<i64>,
    pub venue_ts_ms: i64,
}

/// What one received frame turned out to be.
#[derive(Copy, Clone, Debug, PartialEq)]
pub enum ParsedFrame<'a> {
    Book(BookFrame<'a>),
    Ticker(TickerFrame<'a>),
    /// A subscribe/unsubscribe reply.
    Ack {
        op: &'a str,
        success: bool,
        ret_msg: &'a str,
    },
    /// The keep-alive reply.
    Pong,
    /// A frame the engine has no use for.
    Ignored,
}

/// Parse one frame from text.
pub fn parse_frame(raw: &str) -> Result<ParsedFrame<'_>, FeedError> {
    let env: Envelope<'_> =
        serde_json::from_str(raw).map_err(|e| FeedError::BadMessage(e.to_string()))?;
    frame_from(env)
}

/// Parse one frame from bytes, for binary websocket messages.
pub fn parse_frame_bytes(raw: &[u8]) -> Result<ParsedFrame<'_>, FeedError> {
    let env: Envelope<'_> =
        serde_json::from_slice(raw).map_err(|e| FeedError::BadMessage(e.to_string()))?;
    frame_from(env)
}

fn frame_from(env: Envelope<'_>) -> Result<ParsedFrame<'_>, FeedError> {
    // Control frames answer an `op` and carry no topic.
    if let Some(op) = env.op {
        return Ok(match op {
            "ping" | "pong" => ParsedFrame::Pong,
            "subscribe" | "unsubscribe" => ParsedFrame::Ack {
                op,
                success: env.success.unwrap_or(false),
                ret_msg: env.ret_msg.unwrap_or(""),
            },
            _ => ParsedFrame::Ignored,
        });
    }

    let Some(topic) = env.topic else {
        return Ok(ParsedFrame::Ignored);
    };
    let Some(symbol) = topic.rsplit_once('.').map(|(_, s)| s) else {
        return Ok(ParsedFrame::Ignored);
    };
    if symbol.is_empty() {
        return Ok(ParsedFrame::Ignored);
    }
    let snapshot = env.msg_type != Some("delta");
    let data = env.data.unwrap_or_default();

    if topic.starts_with("orderbook.") {
        let Some(update_id) = data.u else {
            return Err(FeedError::BadMessage(format!(
                "orderbook frame for {symbol} has no update id"
            )));
        };
        // `cts` is matching-engine time, closer to when the book actually
        // changed; `ts` is when the gateway sent it.
        let venue_ts_ms = env.cts.or(env.ts).unwrap_or(0);
        return Ok(ParsedFrame::Book(BookFrame {
            symbol,
            snapshot: snapshot || update_id == 1,
            update_id,
            bids: data.b,
            asks: data.a,
            venue_ts_ms,
        }));
    }

    if topic.starts_with("tickers.") {
        return Ok(ParsedFrame::Ticker(TickerFrame {
            symbol,
            snapshot,
            last_px: data.last_price,
            mark_px: data.mark_price,
            index_px: data.index_price,
            funding_rate: data.funding_rate,
            next_funding_ms: data.next_funding_time,
            venue_ts_ms: env.ts.unwrap_or(0),
        }));
    }

    Ok(ParsedFrame::Ignored)
}

#[derive(Deserialize)]
struct Envelope<'a> {
    #[serde(borrow, default)]
    topic: Option<&'a str>,
    #[serde(borrow, default)]
    op: Option<&'a str>,
    #[serde(rename = "type", borrow, default)]
    msg_type: Option<&'a str>,
    #[serde(default)]
    ts: Option<i64>,
    #[serde(default)]
    cts: Option<i64>,
    #[serde(default)]
    success: Option<bool>,
    #[serde(borrow, default)]
    ret_msg: Option<&'a str>,
    #[serde(default)]
    data: Option<DataFields>,
}

/// The union of the two payload shapes the engine subscribes to. Orderbook
/// and ticker messages share no field name, so one struct reads either in a
/// single pass over the bytes.
#[derive(Deserialize, Default)]
struct DataFields {
    #[serde(default)]
    b: Option<Levels>,
    #[serde(default)]
    a: Option<Levels>,
    #[serde(default)]
    u: Option<u64>,

    #[serde(rename = "lastPrice", default, deserialize_with = "opt_f64")]
    last_price: Option<f64>,
    #[serde(rename = "markPrice", default, deserialize_with = "opt_f64")]
    mark_price: Option<f64>,
    #[serde(rename = "indexPrice", default, deserialize_with = "opt_f64")]
    index_price: Option<f64>,
    #[serde(rename = "fundingRate", default, deserialize_with = "opt_f64")]
    funding_rate: Option<f64>,
    #[serde(rename = "nextFundingTime", default, deserialize_with = "opt_i64")]
    next_funding_time: Option<i64>,
}

impl<'de> Deserialize<'de> for Levels {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Levels;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("an array of [price, quantity] pairs")
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Levels, A::Error> {
                let mut out = Levels::default();
                while let Some(level) = seq.next_element::<Level>()? {
                    if out.len == 0 {
                        out.best = Some(level);
                    }
                    out.len = out.len.saturating_add(1);
                }
                Ok(out)
            }
        }
        d.deserialize_seq(V)
    }
}

impl<'de> Deserialize<'de> for Level {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Level;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a [price, quantity] pair")
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Level, A::Error> {
                let px: Num = seq
                    .next_element()?
                    .ok_or_else(|| de::Error::invalid_length(0, &"a price"))?;
                let qty: Num = seq
                    .next_element()?
                    .ok_or_else(|| de::Error::invalid_length(1, &"a quantity"))?;
                while seq.next_element::<de::IgnoredAny>()?.is_some() {}
                Ok(Level {
                    px: px.0,
                    qty: qty.0,
                })
            }
        }
        d.deserialize_seq(V)
    }
}

/// A number the venue sends as a string.
struct Num(f64);

impl<'de> Deserialize<'de> for Num {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_any(NumVisitor).and_then(|v| {
            v.map(Num)
                .ok_or_else(|| de::Error::custom("expected a number, found nothing"))
        })
    }
}

fn opt_f64<'de, D: Deserializer<'de>>(d: D) -> Result<Option<f64>, D::Error> {
    d.deserialize_any(NumVisitor)
}

fn opt_i64<'de, D: Deserializer<'de>>(d: D) -> Result<Option<i64>, D::Error> {
    Ok(d.deserialize_any(NumVisitor)?.map(|v| v as i64))
}

/// Reads a number whether it arrives as a JSON string or a JSON number.
/// Bybit sends prices, sizes and times as strings, and uses `""` for a field
/// that does not apply, which reads as absent.
struct NumVisitor;

impl<'de> Visitor<'de> for NumVisitor {
    type Value = Option<f64>;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("a number, or a string holding one")
    }

    fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
        let v = v.trim();
        if v.is_empty() {
            return Ok(None);
        }
        v.parse::<f64>()
            .map(Some)
            .map_err(|_| de::Error::custom(format!("not a number: {v}")))
    }

    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Self::Value, E> {
        Ok(Some(v))
    }

    fn visit_i64<E: de::Error>(self, v: i64) -> Result<Self::Value, E> {
        Ok(Some(v as f64))
    }

    fn visit_u64<E: de::Error>(self, v: u64) -> Result<Self::Value, E> {
        Ok(Some(v as f64))
    }

    fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(None)
    }

    fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(None)
    }

    fn visit_some<D: Deserializer<'de>>(self, d: D) -> Result<Self::Value, D::Error> {
        d.deserialize_any(NumVisitor)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Captured from the venue's public linear stream on 2026-08-13.
    const OB_SNAPSHOT: &str = r#"{"topic":"orderbook.1.BTCUSDT","ts":1786659828109,"type":"snapshot","data":{"s":"BTCUSDT","b":[["63561.5","1.776"]],"a":[["63561.6","4.848"]],"u":31398762,"seq":764144453970},"cts":1786659828107}"#;
    const TK_SNAPSHOT: &str = r#"{"topic":"tickers.BTCUSDT","type":"snapshot","data":{"symbol":"BTCUSDT","tickDirection":"ZeroPlusTick","price24hPcnt":"0.001455","lastPrice":"63561.60","prevPrice24h":"63469.20","highPrice24h":"63994.40","lowPrice24h":"62810.50","prevPrice1h":"63413.60","markPrice":"63560.80","indexPrice":"63585.51","openInterest":"59972.256","openInterestValue":"3811884569.16","turnover24h":"3515096831.9640","volume24h":"55362.9100","fundingIntervalHour":"8","fundingCap":"0.005","singleOpenInterest":"29986.128","singleOpenInterestValue":"1905942284.58","nextFundingTime":"1786665600000","fundingRate":"0.00005013","bid1Price":"63561.50","bid1Size":"1.760","ask1Price":"63561.60","ask1Size":"4.690","preOpenPrice":"","preQty":"","curPreListingPhase":""},"cs":764144442982,"ts":1786659827683}"#;
    const TK_DELTA: &str = r#"{"topic":"tickers.BTCUSDT","type":"delta","data":{"symbol":"BTCUSDT","indexPrice":"63585.74"},"cs":764144450620,"ts":1786659827983}"#;
    const ACK: &str = r#"{"success":true,"ret_msg":"","conn_id":"d9tc7k421nl91l9k6obg-s09y","req_id":"t1","op":"subscribe"}"#;
    const PONG: &str = r#"{"success":true,"ret_msg":"pong","conn_id":"d9tc7k421nl91l9k6obg-s09y","req_id":"p1","op":"ping"}"#;

    fn book(raw: &str) -> BookFrame<'_> {
        match parse_frame(raw).expect("parses") {
            ParsedFrame::Book(b) => b,
            other => panic!("expected a book frame, got {other:?}"),
        }
    }

    fn ticker(raw: &str) -> TickerFrame<'_> {
        match parse_frame(raw).expect("parses") {
            ParsedFrame::Ticker(t) => t,
            other => panic!("expected a ticker frame, got {other:?}"),
        }
    }

    #[test]
    fn orderbook_snapshot_reads_both_sides() {
        let b = book(OB_SNAPSHOT);
        assert_eq!(b.symbol, "BTCUSDT");
        assert!(b.snapshot);
        assert_eq!(b.update_id, 31398762);
        // cts wins over ts: it is matching-engine time.
        assert_eq!(b.venue_ts_ms, 1786659828107);
        assert_eq!(b.bids.unwrap().best().unwrap().px, 63561.5);
        assert_eq!(b.bids.unwrap().best().unwrap().qty, 1.776);
        assert_eq!(b.asks.unwrap().best().unwrap().px, 63561.6);
        assert_eq!(b.asks.unwrap().best().unwrap().qty, 4.848);
    }

    #[test]
    fn one_sided_delta_leaves_the_other_side_absent() {
        let raw = r#"{"topic":"orderbook.1.BTCUSDT","ts":1786659828200,"type":"delta","data":{"s":"BTCUSDT","b":[["63561.4","2.100"]],"a":[],"u":31398763},"cts":1786659828199}"#;
        let b = book(raw);
        assert!(!b.snapshot);
        assert_eq!(b.bids.unwrap().best().unwrap().px, 63561.4);
        // An empty array is "this side did not change", distinct from absent.
        assert!(b.asks.unwrap().is_empty());

        let missing = r#"{"topic":"orderbook.1.BTCUSDT","ts":1,"type":"delta","data":{"s":"BTCUSDT","b":[["63561.4","2.100"]],"u":31398764},"cts":1}"#;
        assert!(book(missing).asks.is_none());
    }

    #[test]
    fn zero_quantity_level_survives_the_parse() {
        // The parse reports it faithfully; removal is the state layer's job.
        let raw = r#"{"topic":"orderbook.1.BTCUSDT","ts":1,"type":"delta","data":{"s":"BTCUSDT","b":[["63561.5","0"]],"a":[],"u":31398765},"cts":1}"#;
        let b = book(raw);
        let best = b.bids.unwrap().best().unwrap();
        assert_eq!(best.px, 63561.5);
        assert_eq!(best.qty, 0.0);
    }

    #[test]
    fn update_id_one_is_a_service_restart_snapshot() {
        let raw = r#"{"topic":"orderbook.1.BTCUSDT","ts":1,"type":"delta","data":{"s":"BTCUSDT","b":[["1.0","1.0"]],"a":[["2.0","1.0"]],"u":1},"cts":1}"#;
        assert!(book(raw).snapshot);
    }

    #[test]
    fn tickers_snapshot_reads_the_fields_of_interest() {
        let t = ticker(TK_SNAPSHOT);
        assert_eq!(t.symbol, "BTCUSDT");
        assert!(t.snapshot);
        assert_eq!(t.last_px, Some(63561.60));
        assert_eq!(t.mark_px, Some(63560.80));
        assert_eq!(t.index_px, Some(63585.51));
        assert_eq!(t.funding_rate, Some(0.00005013));
        assert_eq!(t.next_funding_ms, Some(1786665600000));
        assert_eq!(t.venue_ts_ms, 1786659827683);
    }

    #[test]
    fn tickers_delta_reports_only_what_changed() {
        let t = ticker(TK_DELTA);
        assert!(!t.snapshot);
        assert_eq!(t.index_px, Some(63585.74));
        assert_eq!(t.last_px, None);
        assert_eq!(t.mark_px, None);
        assert_eq!(t.funding_rate, None);
        assert_eq!(t.next_funding_ms, None);
    }

    #[test]
    fn empty_string_field_reads_as_absent() {
        let raw = r#"{"topic":"tickers.BTCUSDT","type":"delta","data":{"symbol":"BTCUSDT","fundingRate":"","markPrice":"12.5"},"ts":7}"#;
        let t = ticker(raw);
        assert_eq!(t.funding_rate, None);
        assert_eq!(t.mark_px, Some(12.5));
    }

    #[test]
    fn subscription_ack_is_not_a_market_event() {
        assert_eq!(
            parse_frame(ACK).unwrap(),
            ParsedFrame::Ack {
                op: "subscribe",
                success: true,
                ret_msg: ""
            }
        );
        let failed = r#"{"success":false,"ret_msg":"Invalid symbol","conn_id":"x","req_id":"t1","op":"subscribe"}"#;
        assert_eq!(
            parse_frame(failed).unwrap(),
            ParsedFrame::Ack {
                op: "subscribe",
                success: false,
                ret_msg: "Invalid symbol"
            }
        );
    }

    #[test]
    fn pong_is_not_a_market_event() {
        assert_eq!(parse_frame(PONG).unwrap(), ParsedFrame::Pong);
        assert_eq!(parse_frame(r#"{"op":"pong"}"#).unwrap(), ParsedFrame::Pong);
    }

    #[test]
    fn unknown_topics_and_junk_are_handled() {
        assert_eq!(
            parse_frame(r#"{"topic":"publicTrade.BTCUSDT","data":[]}"#).unwrap(),
            ParsedFrame::Ignored
        );
        assert!(matches!(
            parse_frame("not json"),
            Err(FeedError::BadMessage(_))
        ));
        // A book frame with no update id cannot be sequenced, so it is a fault.
        let no_u = r#"{"topic":"orderbook.1.BTCUSDT","ts":1,"type":"delta","data":{"s":"BTCUSDT","b":[]},"cts":1}"#;
        assert!(matches!(parse_frame(no_u), Err(FeedError::BadMessage(_))));
    }

    #[test]
    fn bytes_and_text_agree() {
        assert_eq!(
            parse_frame(OB_SNAPSHOT).unwrap(),
            parse_frame_bytes(OB_SNAPSHOT.as_bytes()).unwrap()
        );
    }

    #[test]
    fn a_deeper_push_keeps_the_best_level_and_counts_the_rest() {
        let raw = r#"{"topic":"orderbook.50.BTCUSDT","ts":1,"type":"snapshot","data":{"s":"BTCUSDT","b":[["10","1"],["9","2"],["8","3"]],"a":[],"u":5},"cts":1}"#;
        let bids = book(raw).bids.unwrap();
        assert_eq!(bids.best(), Some(Level { px: 10.0, qty: 1.0 }));
        assert_eq!(bids.len(), 3);
    }
}
