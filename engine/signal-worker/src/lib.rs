//! Credential-free LONG and CARRY public-data worker.
//!
//! The process is deliberately outside the synchronous account and order loop.
//! It accepts public venue observations, enforces their knowledge times, builds
//! the registered rolling features, and emits typed observations for native
//! reducers. It has no account, key, signer, or order type.

pub mod bybit_ws;
pub mod config;
pub mod features;
pub mod http;
pub mod live;
pub mod model;
pub mod normalize;
pub mod store;
pub mod universe;
pub mod worker;

pub use config::{ConfigIdentity, SignalWorkerConfig};
pub use model::{NormalizedObservation, WireEvent};
pub use worker::{DurableSignalWorker, SignalWorker, WorkerError, WorkerState};

pub const SCHEMA_VERSION: u32 = 1;
pub const HOUR_MS: i64 = 3_600_000;
pub const DAY_MS: i64 = 86_400_000;
