//! One test binary for the venue adapters. `arming_env.rs` stays on its own:
//! it mutates process environment variables.

mod support;
#[path = "../../../test-support/io.rs"]
mod test_io;

mod adapter_semantics;
#[cfg(feature = "binance")]
mod binance_requests;
mod conformance;
mod dormant_venues;
mod exact_account_stops;
mod exact_stops;
#[cfg(feature = "hyperliquid")]
mod hyperliquid_requests;
#[cfg(feature = "lighter")]
mod lighter_requests;
#[cfg(feature = "mexc")]
mod mexc_audit;
#[cfg(feature = "mexc")]
mod mexc_exact_orders;
#[cfg(feature = "mexc")]
mod mexc_private_stream;
mod order_lookup_lane;
mod order_lookups;
#[cfg(feature = "bybit")]
mod private_stream;
#[cfg(feature = "bybit")]
mod recorded_private;
mod registry_forwarding;
#[cfg(feature = "bybit")]
mod request_shape;
mod venue_fence;
#[cfg(feature = "bybit")]
mod venue_registry;
