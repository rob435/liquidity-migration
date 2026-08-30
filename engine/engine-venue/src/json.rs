//! Reading fields out of a venue's JSON, the same way for every venue.
//!
//! Venues disagree about almost everything, but they all send numbers that
//! have to become `f64` and strings that have to be there. A field we cannot
//! read is a `BadReply` — never a zero, never a default. Guessing here would
//! hand the risk kernel a picture of an account that does not exist.
//!
//! Shared rather than copied per adapter: four copies of "blank means absent"
//! is four chances for one of them to mean something else.

use engine_types::VenueError;
use serde_json::Value;

pub(crate) fn str_field(obj: &Value, name: &str) -> Result<String, VenueError> {
    obj.get(name)
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| VenueError::BadReply(format!("field {name} is missing or not a string")))
}

pub(crate) fn num_field(obj: &Value, name: &str) -> Result<f64, VenueError> {
    opt_num_field(obj, name)?
        .ok_or_else(|| VenueError::BadReply(format!("field {name} is missing or blank")))
}

/// `None` means present-but-blank or absent; an unparseable value is an error.
pub(crate) fn opt_num_field(obj: &Value, name: &str) -> Result<Option<f64>, VenueError> {
    match obj.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.trim().is_empty() => Ok(None),
        Some(Value::String(s)) => match s.trim().parse::<f64>() {
            Ok(value) if value.is_finite() => Ok(Some(value)),
            _ => Err(VenueError::BadReply(format!(
                "field {name} is not a finite number: {s:?}"
            ))),
        },
        Some(Value::Number(n)) => n
            .as_f64()
            .map(Some)
            .ok_or_else(|| VenueError::BadReply(format!("field {name} is not a finite number"))),
        Some(other) => Err(VenueError::BadReply(format!(
            "field {name} is a {}, not a number",
            kind_of(other)
        ))),
    }
}

/// An integer field, refusing a value that is not one. Hyperliquid and Lighter
/// both key orders by integer ids, and a rounded float id is a cancel aimed at
/// somebody else's order.
pub(crate) fn int_field(obj: &Value, name: &str) -> Result<i64, VenueError> {
    match obj.get(name) {
        Some(Value::Number(n)) => n
            .as_i64()
            .ok_or_else(|| VenueError::BadReply(format!("field {name} is not a whole number"))),
        Some(Value::String(s)) => s.trim().parse::<i64>().map_err(|_| {
            VenueError::BadReply(format!("field {name} is not a whole number: {s:?}"))
        }),
        _ => Err(VenueError::BadReply(format!(
            "field {name} is missing or not a whole number"
        ))),
    }
}

pub(crate) fn kind_of(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_blank_string_is_absent_and_a_bad_one_is_an_error() {
        let row = json!({
            "blank": "", "good": "1.5", "bad": "abc", "n": 2,
            "nan": "NaN", "infinity": "inf"
        });
        assert_eq!(opt_num_field(&row, "blank").unwrap(), None);
        assert_eq!(opt_num_field(&row, "missing").unwrap(), None);
        assert_eq!(opt_num_field(&row, "good").unwrap(), Some(1.5));
        assert_eq!(opt_num_field(&row, "n").unwrap(), Some(2.0));
        assert!(opt_num_field(&row, "bad").is_err());
        assert!(opt_num_field(&row, "nan").is_err());
        assert!(opt_num_field(&row, "infinity").is_err());
        // The required form refuses what the optional one calls absent.
        assert!(num_field(&row, "blank").is_err());
    }

    #[test]
    fn an_integer_id_is_never_rounded_into_place() {
        let row = json!({"oid": 42, "as_text": "43", "fractional": 44.5});
        assert_eq!(int_field(&row, "oid").unwrap(), 42);
        assert_eq!(int_field(&row, "as_text").unwrap(), 43);
        assert!(
            int_field(&row, "fractional").is_err(),
            "a fractional order id must not be rounded into a cancel"
        );
    }
}
