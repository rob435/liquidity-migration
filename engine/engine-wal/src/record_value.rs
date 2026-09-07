use serde::de::{self, MapAccess, SeqAccess, Visitor};
use serde::{Deserialize, Deserializer};
use serde_json::{Map, Number, Value};
use std::fmt;

// Value's map normally overwrites duplicate keys. Keep the typed reader's
// duplicate-field refusal while sharing one parsed value with version checks.
struct RecordValue(Value);

impl<'de> Deserialize<'de> for RecordValue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct RecordVisitor;

        impl<'de> Visitor<'de> for RecordVisitor {
            type Value = RecordValue;

            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("a WAL JSON value with unique object fields")
            }

            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(RecordValue(Value::Null))
            }

            fn visit_bool<E: de::Error>(self, value: bool) -> Result<Self::Value, E> {
                Ok(RecordValue(Value::Bool(value)))
            }

            fn visit_i64<E: de::Error>(self, value: i64) -> Result<Self::Value, E> {
                Ok(RecordValue(Value::Number(value.into())))
            }

            fn visit_u64<E: de::Error>(self, value: u64) -> Result<Self::Value, E> {
                Ok(RecordValue(Value::Number(value.into())))
            }

            fn visit_f64<E: de::Error>(self, value: f64) -> Result<Self::Value, E> {
                Number::from_f64(value)
                    .map(|number| RecordValue(Value::Number(number)))
                    .ok_or_else(|| E::custom("nonfinite WAL JSON number"))
            }

            fn visit_str<E: de::Error>(self, value: &str) -> Result<Self::Value, E> {
                self.visit_string(value.to_owned())
            }

            fn visit_string<E: de::Error>(self, value: String) -> Result<Self::Value, E> {
                Ok(RecordValue(Value::String(value)))
            }

            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut values = Vec::new();
                while let Some(RecordValue(value)) = seq.next_element()? {
                    values.push(value);
                }
                Ok(RecordValue(Value::Array(values)))
            }

            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut values = Map::new();
                while let Some(key) = map.next_key::<String>()? {
                    if values.contains_key(&key) {
                        return Err(de::Error::custom(format!("duplicate field `{key}`")));
                    }
                    let RecordValue(value) = map.next_value()?;
                    values.insert(key, value);
                }
                Ok(RecordValue(Value::Object(values)))
            }
        }

        deserializer.deserialize_any(RecordVisitor)
    }
}

pub(super) fn parse(payload: &[u8]) -> Result<Value, serde_json::Error> {
    serde_json::from_slice::<RecordValue>(payload).map(|value| value.0)
}
