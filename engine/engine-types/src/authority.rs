//! Whether a venue command queued a moment ago may still be transmitted.
//!
//! An opening is judged when the engine hands it to the venue task and sent
//! when the task reaches it. In between it waits behind other commands and
//! then behind the adapter's own request quota, and a halt, a lost private
//! stream, a tripped loss limit or a replaced strategy in that interval must
//! stop it going out — cancelling it afterwards is not the same trade.
//!
//! The venue task and the paced adapters read the same predicate here, so
//! one cannot send what the other would refuse.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;

/// The permission one queued mutation was minted under.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct CommandAuthority {
    /// What [`AuthorityEpoch::current`] read when the command was queued.
    pub epoch: u64,
    /// Monotonic ns from [`crate::clock::mono_ns`] at enqueue.
    pub queued_ns: u64,
    /// `queued_ns` plus `engine.opening_dispatch_ttl_ms`.
    pub expires_at_ns: u64,
}

/// The engine's opening permission as one number the venue task can read
/// without taking a lock: bumped whenever an opening decided a moment ago
/// would no longer be admitted.
///
/// Cheap to clone; every clone reads and writes the same counter.
#[derive(Clone, Debug)]
pub struct AuthorityEpoch(Arc<AtomicU64>);

impl Default for AuthorityEpoch {
    fn default() -> Self {
        Self::new()
    }
}

impl AuthorityEpoch {
    pub fn new() -> Self {
        Self(Arc::new(AtomicU64::new(1)))
    }

    pub fn current(&self) -> u64 {
        self.0.load(Ordering::Acquire)
    }

    /// Retire every opening still queued under the current epoch, and return
    /// the epoch that replaces it.
    pub fn advance(&self) -> u64 {
        self.0.fetch_add(1, Ordering::AcqRel).wrapping_add(1)
    }
}

/// Why this command must not be transmitted, in the words that reach the
/// order's reject reason. `None` means send it.
pub fn authority_refusal(
    shared: &AuthorityEpoch,
    authority: CommandAuthority,
    now_ns: u64,
) -> Option<String> {
    let current = shared.current();
    if authority.epoch != current {
        return Some(format!(
            "authority: epoch {} superseded by {current}",
            authority.epoch
        ));
    }
    (now_ns >= authority.expires_at_ns).then(|| {
        format!(
            "authority: expired after {} ms in the venue queue",
            now_ns.saturating_sub(authority.queued_ns) / 1_000_000
        )
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minted(shared: &AuthorityEpoch, queued_ns: u64, ttl_ns: u64) -> CommandAuthority {
        CommandAuthority {
            epoch: shared.current(),
            queued_ns,
            expires_at_ns: queued_ns + ttl_ns,
        }
    }

    #[test]
    fn a_command_minted_under_the_live_epoch_inside_its_ttl_may_be_sent() {
        let shared = AuthorityEpoch::new();
        let authority = minted(&shared, 1_000, 10_000);
        assert_eq!(authority_refusal(&shared, authority, 10_999), None);
    }

    #[test]
    fn an_advanced_epoch_supersedes_a_command_that_has_not_expired() {
        let shared = AuthorityEpoch::new();
        let authority = minted(&shared, 1_000, u64::MAX - 1_000);
        assert_eq!(shared.advance(), 2);
        let reason = authority_refusal(&shared, authority, 1_001).expect("superseded");
        assert_eq!(reason, "authority: epoch 1 superseded by 2");
    }

    #[test]
    fn an_expired_command_names_how_long_it_waited() {
        let shared = AuthorityEpoch::new();
        let authority = minted(&shared, 1_000_000, 10_000_000);
        let reason = authority_refusal(&shared, authority, 25_000_000).expect("expired");
        assert_eq!(reason, "authority: expired after 24 ms in the venue queue");
    }

    #[test]
    fn every_clone_of_one_epoch_reads_the_same_counter() {
        let shared = AuthorityEpoch::new();
        let worker = shared.clone();
        assert_eq!(worker.current(), 1);
        shared.advance();
        assert_eq!(worker.current(), 2);
    }
}
