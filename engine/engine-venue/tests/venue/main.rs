//! One test binary for the venue adapters. `arming_env.rs` stays on its own:
//! it mutates process environment variables.

mod support;

mod binance_requests;
mod dormant_venues;
mod exact_account_stops;
mod exact_stops;
mod hyperliquid_requests;
mod lighter_requests;
mod mexc_exact_orders;
mod order_lookup_lane;
mod order_lookups;
mod private_stream;
mod registry_forwarding;
mod request_shape;
mod venue_fence;
mod venue_registry;
