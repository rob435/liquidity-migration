//! Reading one strategy's TOML parameters. Every failure names the parameter,
//! because the config was written by the research system and the operator
//! reading the error needs to know which line to fix.

use crate::BuildError;

pub(crate) struct Params<'a> {
    strategy: &'static str,
    table: &'a toml::Table,
}

impl<'a> Params<'a> {
    pub(crate) fn new(strategy: &'static str, value: &'a toml::Value) -> Result<Self, BuildError> {
        match value.as_table() {
            Some(table) => Ok(Self { strategy, table }),
            None => Err(BuildError::ParamsNotATable { strategy, got: type_name(value) }),
        }
    }

    pub(crate) fn invalid(&self, param: &'static str, detail: impl Into<String>) -> BuildError {
        BuildError::InvalidParam { strategy: self.strategy, param, detail: detail.into() }
    }

    /// Refuse any key outside the strategy's read set, naming both the key
    /// and the set. Call last, after every read succeeded.
    pub(crate) fn reject_unknown(&self, known: &'static [&'static str]) -> Result<(), BuildError> {
        for key in self.table.keys() {
            if !known.contains(&key.as_str()) {
                return Err(BuildError::UnknownParam {
                    strategy: self.strategy,
                    param: key.clone(),
                    known,
                });
            }
        }
        Ok(())
    }

    fn get(&self, param: &'static str) -> Result<&'a toml::Value, BuildError> {
        self.table
            .get(param)
            .ok_or(BuildError::MissingParam { strategy: self.strategy, param })
    }

    pub(crate) fn string(&self, param: &'static str) -> Result<String, BuildError> {
        let value = self.get(param)?;
        match value.as_str() {
            Some(s) => Ok(s.to_string()),
            None => Err(self.invalid(param, format!("expected a string, got {}", type_name(value)))),
        }
    }

    /// A list of non-empty strings. An empty entry is refused rather than
    /// dropped: a blank symbol in a list is a mistake in the config, and
    /// silently trading one fewer name is the worst way to answer it.
    pub(crate) fn strings(&self, param: &'static str) -> Result<Vec<String>, BuildError> {
        let value = self.get(param)?;
        let Some(items) = value.as_array() else {
            return Err(self.invalid(param, format!("expected a list, got {}", type_name(value))));
        };
        let mut out = Vec::with_capacity(items.len());
        for item in items {
            match item.as_str() {
                Some(text) if !text.is_empty() => out.push(text.to_string()),
                Some(_) => return Err(self.invalid(param, "one of the entries is an empty string")),
                None => {
                    return Err(self.invalid(
                        param,
                        format!("expected a list of strings, found {} in it", type_name(item)),
                    ))
                }
            }
        }
        Ok(out)
    }

    /// A finite number, greater than zero. Integers are accepted so a config
    /// may say `qty = 1` rather than `qty = 1.0`.
    pub(crate) fn positive(&self, param: &'static str) -> Result<f64, BuildError> {
        let value = self.get(param)?;
        self.as_positive(param, value)
    }

    pub(crate) fn opt_positive(&self, param: &'static str) -> Result<Option<f64>, BuildError> {
        match self.table.get(param) {
            None => Ok(None),
            Some(value) => self.as_positive(param, value).map(Some),
        }
    }

    pub(crate) fn opt_u64(&self, param: &'static str) -> Result<Option<u64>, BuildError> {
        let Some(value) = self.table.get(param) else {
            return Ok(None);
        };
        let Some(n) = value.as_integer() else {
            return Err(self.invalid(param, format!("expected a whole number, got {}", type_name(value))));
        };
        u64::try_from(n)
            .map(Some)
            .map_err(|_| self.invalid(param, format!("expected a whole number of at least 0, got {n}")))
    }

    fn as_positive(&self, param: &'static str, value: &toml::Value) -> Result<f64, BuildError> {
        let n = match value {
            toml::Value::Float(f) => *f,
            toml::Value::Integer(i) => *i as f64,
            other => {
                return Err(self.invalid(param, format!("expected a number, got {}", type_name(other))))
            }
        };
        if !n.is_finite() {
            return Err(self.invalid(param, format!("expected a real number, got {n}")));
        }
        if n <= 0.0 {
            return Err(self.invalid(param, format!("expected a number above 0, got {n}")));
        }
        Ok(n)
    }
}

fn type_name(value: &toml::Value) -> &'static str {
    match value {
        toml::Value::String(_) => "a string",
        toml::Value::Integer(_) => "an integer",
        toml::Value::Float(_) => "a float",
        toml::Value::Boolean(_) => "a boolean",
        toml::Value::Datetime(_) => "a datetime",
        toml::Value::Array(_) => "an array",
        toml::Value::Table(_) => "a table",
    }
}
