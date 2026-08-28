//! The account loss halt, equity-anchored envelope, account caps, and
//! stop-attach discipline. Unknown state refuses the order.
//!
//! What each rule is, what was simplified, and where this kernel is
//! deliberately stricter than the fleet it was ported from is written down in
//! PORT_NOTES.md next to this file, and pinned by the tests beside it.
//!
//! Every threshold is supplied by [`KernelConfig`]; there are no numbers baked
//! into the code.

mod config;
mod envelope;
mod exposure;
mod kernel;
mod loss_guard;
mod profile;

pub use config::{ConfigError, EnvelopeConfig, KernelConfig, LossGuardConfig};
pub use kernel::Kernel;
pub use loss_guard::{cleared_loss_guard_state, LossGuardAnchor, Trip};
pub use profile::{
    kernel_config_from_profile, ProfileInputs, PROFILE_KIND, PROFILE_SCHEMA_VERSION,
};
