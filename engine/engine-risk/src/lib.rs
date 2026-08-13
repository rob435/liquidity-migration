//! The four capital controls, ported from the Python fleet: the account loss
//! guard, the equity-anchored envelope, the per-strategy capital partition, and
//! the stop-attach discipline. Unknown state refuses the order.
//!
//! The Python originals stay the reference. What each rule became here, what
//! was simplified, and where this kernel is deliberately stricter is written
//! down in PORT_NOTES.md next to this file.
//!
//! Every threshold is supplied by [`KernelConfig`]; there are no numbers baked
//! into the code.

mod config;
mod envelope;
mod exposure;
mod kernel;
mod loss_guard;

pub use config::{
    ConfigError, EnvelopeConfig, KernelConfig, LossGuardConfig, PartitionConfig, StrategyAllocation,
};
pub use kernel::Kernel;
pub use loss_guard::{LossGuardAnchor, Trip};
