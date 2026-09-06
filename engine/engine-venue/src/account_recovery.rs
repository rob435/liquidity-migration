use engine_types::{Symbol, SymbolId, VenueError};
use std::collections::HashMap;

/// Brent's cycle detector retains one digest, irrespective of history length.
#[derive(Default)]
pub(crate) struct PageProgress {
    anchor: Option<[u8; 32]>,
    power: usize,
    distance: usize,
}
impl PageProgress {
    pub fn advance(&mut self, digest: [u8; 32]) -> Result<(), VenueError> {
        if self.anchor == Some(digest) {
            return Err(VenueError::BadReply(
                "execution history pagination repeated a page or cursor".into(),
            ));
        }
        if self.anchor.is_none() || self.distance == self.power {
            self.anchor = Some(digest);
            self.power = self.power.saturating_mul(2).max(1);
            self.distance = 0;
        }
        self.distance = self.distance.saturating_add(1);
        Ok(())
    }
    pub fn cursor(&mut self, cursor: &str) -> Result<(), VenueError> {
        use sha2::Digest;
        self.advance(sha2::Sha256::digest(cursor.as_bytes()).into())
    }
    pub fn rows(&mut self, rows: &[engine_types::VenueExecution]) -> Result<(), VenueError> {
        use sha2::Digest;
        let mut hash = sha2::Sha256::new();
        for row in rows {
            hash.update(row.exec_id.len().to_le_bytes());
            hash.update(row.exec_id.as_bytes());
            hash.update(row.venue_ts_ms.to_le_bytes());
        }
        self.advance(hash.finalize().into())
    }
}

struct CancelSpool(Option<std::sync::Arc<std::sync::atomic::AtomicBool>>);
impl Drop for CancelSpool {
    fn drop(&mut self) {
        if let Some(cancelled) = &self.0 {
            cancelled.store(true, std::sync::atomic::Ordering::Relaxed);
        }
    }
}

pub(crate) async fn append_history(
    mut builder: engine_types::ExecutionHistoryBuilder,
    rows: Vec<engine_types::VenueExecution>,
) -> Result<engine_types::ExecutionHistoryBuilder, VenueError> {
    let mut cancel = CancelSpool(Some(builder.cancellation()));
    let result = tokio::task::spawn_blocking(move || {
        builder.extend(rows)?;
        Ok(builder)
    })
    .await
    .map_err(|error| VenueError::Transport(format!("execution history spool task: {error}")))?;
    cancel.0 = None;
    result
}

pub(crate) async fn finish_history(
    builder: engine_types::ExecutionHistoryBuilder,
) -> Result<engine_types::ExecutionHistory, VenueError> {
    let mut cancel = CancelSpool(Some(builder.cancellation()));
    let result = tokio::task::spawn_blocking(move || builder.finish())
        .await
        .map_err(|error| VenueError::Transport(format!("execution history spool task: {error}")))?;
    cancel.0 = None;
    result
}

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

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn history_pagination_accepts_long_scans_and_rejects_cursor_cycles() {
        let mut progress = PageProgress::default();
        for page in 0..100_000 {
            progress.cursor(&page.to_string()).unwrap();
        }
        for period in 1..=100 {
            let mut progress = PageProgress::default();
            let failure =
                (0..1000).find(|page| progress.cursor(&(page % period).to_string()).is_err());
            assert!(failure.is_some(), "missed cycle with period {period}");
        }
    }
}
