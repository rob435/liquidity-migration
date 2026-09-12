//! Lexical JSON decimal decoding before compatibility binary64 conversion.

use engine_types::numeric::{ExactNumber, NumericProvenance};
use engine_types::VenueError;
use serde::{Deserialize, Deserializer};
use serde_json::value::RawValue;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum DecimalField {
    #[default]
    Absent,
    Number(ExactNumber),
    Invalid(String),
}

impl DecimalField {
    pub fn optional(&self, name: &str) -> Result<Option<ExactNumber>, VenueError> {
        match self {
            Self::Absent => Ok(None),
            Self::Number(number) => Ok(Some(number.clone())),
            Self::Invalid(reason) => Err(VenueError::BadReply(format!("field {name}: {reason}"))),
        }
    }
    pub fn required(&self, name: &str) -> Result<ExactNumber, VenueError> {
        self.optional(name)?
            .ok_or_else(|| VenueError::BadReply(format!("field {name} is missing or blank")))
    }
    /// The compatibility projection: this field as binary64, losing whatever
    /// decimal the venue actually sent.
    ///
    /// Not the normal way to read a number. [`Self::required`] and
    /// [`Self::optional`] keep the venue's own decimal, and everything that
    /// prices, sizes, quantizes or accounts uses those. This exists for the
    /// float-shaped structures still being migrated, and every call site is
    /// listed in `tests/repo/test_engine_numeric_boundary.py` — a new one has
    /// to be added there, in the same diff, on purpose.
    pub fn compat_f64(&self, name: &str) -> Result<f64, VenueError> {
        self.required(name)?
            .value
            .to_f64()
            .map_err(|e| VenueError::BadReply(format!("field {name}: {e}")))
    }
}

impl<'de> Deserialize<'de> for DecimalField {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = Box::<RawValue>::deserialize(deserializer)?;
        let raw = raw.get();
        if raw == "null" {
            return Ok(Self::Absent);
        }
        let text = if raw.starts_with('"') {
            serde_json::from_str::<String>(raw).map_err(serde::de::Error::custom)?
        } else {
            raw.to_owned()
        };
        let text = text.trim();
        if text.is_empty() {
            return Ok(Self::Absent);
        }
        Ok(match ExactNumber::venue_decimal(text) {
            Ok(number) => Self::Number(number),
            Err(error) => Self::Invalid(error.to_string()),
        })
    }
}

pub struct RawObject<T>(pub T);
impl<'de, T: serde::de::DeserializeOwned> Deserialize<'de> for RawObject<T> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = Box::<RawValue>::deserialize(deserializer)?;
        decode_object(raw.get())
            .map(Self)
            .map_err(serde::de::Error::custom)
    }
}

/// Duplicate object keys retain the last raw value, including decimal lexemes.
pub fn decode_object<T: serde::de::DeserializeOwned>(raw: &str) -> Result<T, serde_json::Error> {
    let fields: std::collections::BTreeMap<String, Box<RawValue>> = serde_json::from_str(raw)?;
    serde_json::from_str(&serde_json::to_string(&fields)?)
}

/// For explicit compatibility inputs whose lexical JSON has already been lost.
pub fn legacy_number(value: f64) -> Result<ExactNumber, VenueError> {
    ExactNumber::legacy_binary64(value).map_err(|e| VenueError::BadReply(e.to_string()))
}

pub fn has_venue_precision(number: &ExactNumber) -> bool {
    number.provenance == NumericProvenance::VenueDecimal
}

#[cfg(test)]
mod tests {
    use super::*;
    use engine_types::numeric::Exact;

    #[derive(Deserialize)]
    struct Row {
        #[serde(default)]
        amount: DecimalField,
    }

    #[test]
    fn json_numbers_and_escaped_strings_preserve_venue_decimal_precision() {
        for text in [
            r#"{"amount":9007199254740993.0000000000000000001}"#,
            r#"{"amount":"9007199254740993.000000000000000000\u0031"}"#,
        ] {
            let row: Row = serde_json::from_str(text).unwrap();
            let amount = row.amount.required("amount").unwrap();
            assert_eq!(
                amount.value,
                Exact::parse_decimal("9007199254740993.0000000000000000001").unwrap()
            );
            assert!(has_venue_precision(&amount));
        }
    }

    #[test]
    fn absent_zero_malformed_and_unrepresentable_amounts_remain_distinct() {
        for text in ["{}", r#"{"amount":null}"#, r#"{"amount":"  "}"#] {
            let row: Row = serde_json::from_str(text).unwrap();
            assert_eq!(row.amount.optional("amount").unwrap(), None);
            assert!(row.amount.required("amount").is_err());
        }
        let row: Row = serde_json::from_str(r#"{"amount":0}"#).unwrap();
        assert!(row.amount.required("amount").unwrap().value.is_zero());
        for text in [
            r#"{"amount":"NaN"}"#,
            r#"{"amount":true}"#,
            r#"{"amount":[]}"#,
            r#"{"amount":1e99999999999999}"#,
        ] {
            let row: Row = serde_json::from_str(text).unwrap();
            assert!(row.amount.optional("amount").is_err());
        }
        let row: Row = serde_json::from_str(r#"{"amount":1e-400}"#).unwrap();
        assert!(!row.amount.required("amount").unwrap().value.is_zero());
        assert!(row.amount.compat_f64("amount").is_err());
    }
}

#[derive(Debug, Default)]
pub enum IntegerField {
    #[default]
    Absent,
    Number(i64),
    Invalid,
}
impl IntegerField {
    pub fn required(&self, name: &str) -> Result<i64, VenueError> {
        match self {
            Self::Number(value) => Ok(*value),
            _ => Err(VenueError::BadReply(format!(
                "field {name} is missing or not a whole number"
            ))),
        }
    }
}
impl<'de> Deserialize<'de> for IntegerField {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = serde_json::Value::deserialize(deserializer)?;
        Ok(match value {
            serde_json::Value::Null => Self::Absent,
            serde_json::Value::String(text) => text
                .trim()
                .parse::<i64>()
                .map(Self::Number)
                .unwrap_or(Self::Invalid),
            serde_json::Value::Number(number) => {
                number.as_i64().map(Self::Number).unwrap_or(Self::Invalid)
            }
            _ => Self::Invalid,
        })
    }
}
