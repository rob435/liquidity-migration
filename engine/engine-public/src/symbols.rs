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
pub fn intern(ids: &Arc<RwLock<HashMap<String, SymbolId>>>, symbol: &str) -> SymbolId {
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

/// Names retain the caller's exact order and spelling, including legacy duplicates.
/// The reverse map retains the historical last occurrence for duplicate names.
#[derive(Clone, Debug)]
pub struct SymbolCatalog {
    names: Vec<String>,
    ids: HashMap<String, SymbolId>,
}
impl SymbolCatalog {
    pub fn from_names(names: Vec<String>) -> Self {
        let ids = indexed_names(&names);
        Self { names, ids }
    }
    pub fn names(&self) -> &Vec<String> {
        &self.names
    }
    pub fn ids(&self) -> &HashMap<String, SymbolId> {
        &self.ids
    }
    pub fn intern(&mut self, name: &str) -> Option<SymbolId> {
        if let Some(id) = self.ids.get(name) {
            return Some(*id);
        }
        let id = SymbolId(u16::try_from(self.names.len()).ok()?);
        self.names.push(name.to_string());
        self.ids.insert(name.to_string(), id);
        Some(id)
    }
}

/// Legacy dense handles are their positions; reconstruction never sorts or normalizes.
pub fn indexed_names(names: &[String]) -> HashMap<String, SymbolId> {
    names
        .iter()
        .enumerate()
        .map(|(i, name)| (name.clone(), SymbolId(i as u16)))
        .collect()
}

/// Learning an existing name never changes the identity already handed to a consumer.
pub fn learn(ids: &Arc<RwLock<HashMap<String, SymbolId>>>, symbol: &str, id: SymbolId) {
    ids.write()
        .expect("the symbol map lock is poisoned")
        .entry(symbol.to_string())
        .or_insert(id);
}

pub fn resolve(ids: &Arc<RwLock<HashMap<String, SymbolId>>>, symbol: &str) -> Option<SymbolId> {
    ids.read()
        .expect("the symbol map lock is poisoned")
        .get(symbol)
        .copied()
}

#[cfg(test)]
mod catalog_tests {
    use super::*;
    #[test]
    fn legacy_names_replay_without_sorting_or_normalizing() {
        let names = vec!["ETHUSDT".into(), "btcusdt".into(), "ETHUSDT".into()];
        let mut catalog = SymbolCatalog::from_names(names.clone());
        assert_eq!(catalog.names(), &names);
        assert_eq!(catalog.intern("ETHUSDT"), Some(SymbolId(2)));
        assert_eq!(catalog.intern("BTCUSDT"), Some(SymbolId(3)));
        let restored = SymbolCatalog::from_names(catalog.names().clone());
        assert_eq!(restored.names(), catalog.names());
        assert_eq!(restored.ids(), catalog.ids());
    }
    #[test]
    fn exhausted_catalog_keeps_existing_ids_and_refuses_new_without_mutation() {
        let mut catalog =
            SymbolCatalog::from_names((0..=u16::MAX).map(|i| format!("S{i}")).collect());
        assert_eq!(catalog.intern("S65535"), Some(SymbolId(u16::MAX)));
        assert_eq!(catalog.intern("NEXT"), None);
        assert_eq!(catalog.names().len(), 65536);
        assert_eq!(catalog.ids().len(), 65536);
    }
    #[test]
    fn private_learning_preserves_the_first_identity() {
        let ids = Arc::new(RwLock::new(HashMap::new()));
        learn(&ids, "BTC", SymbolId(7));
        learn(&ids, "BTC", SymbolId(9));
        assert_eq!(resolve(&ids, "BTC"), Some(SymbolId(7)));
        assert_eq!(resolve(&ids, "btc"), None);
    }
}
