//! The equity-anchored envelope, account caps, and stop-attach discipline.
//! Unknown state refuses the order.
//!
//! What each rule is and where it refuses is written down on [`Kernel`] and
//! pinned by the tests beside this crate.
//!
//! Every threshold is supplied by [`KernelConfig`]. The one number in the code
//! is [`ROLLING_LOSS_WINDOW_MS`], which is a contract rather than a dial.

mod config;
mod envelope;
mod exposure;
mod kernel;
mod loss_window;
mod profile;

pub use config::{ConfigError, EnvelopeConfig, KernelConfig};
pub use kernel::Kernel;
pub use profile::{
    kernel_config_from_profile, ProfileInputs, PROFILE_KIND, PROFILE_SCHEMA_VERSION,
};

/// How far back the rolling loss window looks, in venue wall-clock
/// milliseconds: one day.
pub const ROLLING_LOSS_WINDOW_MS: i64 = 86_400_000;
