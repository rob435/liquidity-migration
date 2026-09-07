//! Materialize existing accounting semantics for offline WAL conversion.

use std::path::Path;

use engine_core::execution::Fills;
use engine_wal::conversion::Conversion;

pub fn convert(input: &Path, output_dir: &Path) -> Result<Conversion, engine_types::WalError> {
    engine_wal::conversion::v5_to_v7(input, output_dir, |base| {
        Ok(Fills::try_from_records(std::slice::from_ref(base))?.open_trade_lots())
    })
}

#[cfg(test)]
mod tests;
