//! Wire optionals retain legacy wrong-type and absent semantics at one boundary.
use serde::de::DeserializeOwned;
use serde::{Deserialize, Deserializer};
use serde_json::Value;

#[derive(Debug)]
pub(crate) struct Field<T>(pub(crate) Option<T>);
impl<T> Default for Field<T> {
    fn default() -> Self {
        Self(None)
    }
}
impl<'de, T: DeserializeOwned> Deserialize<'de> for Field<T> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let value = Value::deserialize(deserializer)?;
        Ok(Self(serde_json::from_value(value).ok()))
    }
}

pub(crate) fn object<T: DeserializeOwned + Default>(value: &Value) -> T {
    // `Value` resolves repeated keys as last-wins before DTO decoding, matching
    // the existing parser. Non-object control frames remain ignorable.
    T::deserialize(value).unwrap_or_default()
}

#[derive(Debug)]
pub(crate) struct RawField<T>(pub(crate) Option<T>);
impl<T> Default for RawField<T> {
    fn default() -> Self {
        Self(None)
    }
}
impl<'de, T: DeserializeOwned> Deserialize<'de> for RawField<T> {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = Box::<serde_json::value::RawValue>::deserialize(deserializer)?;
        Ok(Self(serde_json::from_str(raw.get()).ok()))
    }
}

pub(crate) fn raw_object<T: DeserializeOwned + Default>(
    text: &str,
) -> Result<T, serde_json::Error> {
    let raw: Box<serde_json::value::RawValue> = serde_json::from_str(text)?;
    if !raw.get().starts_with('{') {
        return Ok(T::default());
    }
    // Only object keys are normalized: duplicate fields keep the last value,
    // while numeric lexemes remain raw until their typed field decodes them.
    let fields: std::collections::BTreeMap<String, Box<serde_json::value::RawValue>> =
        serde_json::from_str(raw.get())?;
    serde_json::from_str(&serde_json::to_string(&fields)?)
}

impl Field<String> {
    pub(crate) fn required(&self, name: &str) -> Result<&str, engine_types::VenueError> {
        self.0.as_deref().ok_or_else(|| {
            engine_types::VenueError::BadReply(format!("field {name} is missing or not a string"))
        })
    }
    pub(crate) fn text(&self) -> &str {
        self.0.as_deref().unwrap_or_default()
    }
}

pub(crate) use engine_public::numeric_wire::IntegerField;

#[cfg(any(
    test,
    feature = "binance",
    feature = "mexc",
    feature = "hyperliquid",
    feature = "lighter"
))]
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub(crate) enum Id {
    Text(String),
    Number(serde_json::Number),
}
#[cfg(any(
    test,
    feature = "binance",
    feature = "mexc",
    feature = "hyperliquid",
    feature = "lighter"
))]
impl Id {
    pub(crate) fn into_text(self) -> String {
        match self {
            Self::Text(s) => s,
            Self::Number(n) => n.to_string(),
        }
    }
}
