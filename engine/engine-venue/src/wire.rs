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

#[derive(Deserialize)]
#[serde(untagged)]
pub(crate) enum Id {
    Text(String),
    Number(serde_json::Number),
}
impl Id {
    pub(crate) fn into_text(self) -> String {
        match self {
            Self::Text(s) => s,
            Self::Number(n) => n.to_string(),
        }
    }
}
