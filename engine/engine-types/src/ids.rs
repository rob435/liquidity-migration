use std::collections::HashMap;

use serde::{Deserialize, Serialize};

/// A venue symbol name, e.g. "BTCUSDT".
pub type Symbol = String;

/// Interned symbol index. The hot path never touches strings; every
/// per-symbol structure is a flat vector indexed by this.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct SymbolId(pub u16);

/// Interned strategy index, assigned at engine boot in config order.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct StrategyId(pub u16);

/// Strategy-chosen timer identity, echoed back on expiry.
#[derive(Copy, Clone, Debug, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct TimerId(pub u32);

/// Bidirectional symbol interning. Built once at boot from the union of all
/// strategy subscriptions; append-only afterwards.
#[derive(Debug, Default)]
pub struct SymbolTable {
    names: Vec<Symbol>,
    index: HashMap<Symbol, SymbolId>,
}

impl SymbolTable {
    pub fn intern(&mut self, name: &str) -> SymbolId {
        if let Some(id) = self.index.get(name) {
            return *id;
        }
        let id = SymbolId(u16::try_from(self.names.len()).expect("more than 65535 symbols"));
        self.names.push(name.to_string());
        self.index.insert(name.to_string(), id);
        id
    }

    pub fn get(&self, name: &str) -> Option<SymbolId> {
        self.index.get(name).copied()
    }

    pub fn name(&self, id: SymbolId) -> &str {
        &self.names[id.0 as usize]
    }

    pub fn len(&self) -> usize {
        self.names.len()
    }

    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }
}
