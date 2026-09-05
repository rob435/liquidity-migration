use engine_types::{Symbol, SymbolId, VenueError};
use std::collections::HashMap;

pub(crate) fn ids(symbols: &[Symbol]) -> Result<HashMap<String, SymbolId>, VenueError> {
    if symbols.len() > engine_types::identity::DENSE_ID_CAPACITY {
        return Err(VenueError::BadRequest(
            "account recovery symbol registry exceeds durable id capacity".into(),
        ));
    }
    let mut ids = HashMap::with_capacity(symbols.len());
    for (index, symbol) in symbols.iter().enumerate() {
        if symbol.is_empty() || ids.insert(symbol.clone(), SymbolId(index as u16)).is_some() {
            return Err(VenueError::BadRequest(
                "account recovery symbol registry is empty or ambiguous".into(),
            ));
        }
    }
    Ok(ids)
}
