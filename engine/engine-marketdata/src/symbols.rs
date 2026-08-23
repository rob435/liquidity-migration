//! The symbol table every feed hands ids out of.
//!
//! One function, shared, because a `SymbolId` is an index assigned by position
//! and every table that maps names to ids has to grow in the same order. Four
//! copies of this rule is four places it can drift, and a feed whose table
//! disagrees with the engine's puts orders on the wrong symbol.

use std::collections::HashMap;
use std::sync::{Arc, RwLock};

use engine_types::SymbolId;

/// This symbol's id, assigning the next one if it is new.
pub(crate) fn intern(ids: &Arc<RwLock<HashMap<String, SymbolId>>>, symbol: &str) -> SymbolId {
    let mut ids = ids.write().expect("the symbol map lock is poisoned");
    if let Some(id) = ids.get(symbol) {
        return *id;
    }
    let id = SymbolId(u16::try_from(ids.len()).expect("more than 65535 symbols"));
    ids.insert(symbol.to_string(), id);
    id
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_are_positions_and_a_repeat_gets_the_one_it_had() {
        let ids = Arc::new(RwLock::new(HashMap::new()));
        assert_eq!(intern(&ids, "BTCUSDT"), SymbolId(0));
        assert_eq!(intern(&ids, "ETHUSDT"), SymbolId(1));
        assert_eq!(intern(&ids, "BTCUSDT"), SymbolId(0));
        assert_eq!(intern(&ids, "SOLUSDT"), SymbolId(2));
    }
}
