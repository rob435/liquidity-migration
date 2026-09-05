//! Reconstruct one registered plug without account, credential or venue access.

use engine_types::strategy_process::{StrategyRuntimeState, STRATEGY_PROCESS_SCHEMA};
use engine_types::Strategy;
use serde::Serialize;
use sha2::{Digest, Sha256};

pub(crate) fn snapshot<T: Serialize, C: Serialize>(
    kind: &str,
    value: &T,
    configuration: &C,
) -> Result<StrategyRuntimeState, String> {
    let value = serde_json::to_value(value).map_err(|error| error.to_string())?;
    let payload = serde_json::to_vec(&value).map_err(|error| error.to_string())?;
    let state = StrategyRuntimeState {
        schema_version: STRATEGY_PROCESS_SCHEMA,
        kind: kind.into(),
        configuration_sha256: hex::encode(Sha256::digest(
            serde_json::to_vec(
                &serde_json::to_value(configuration).map_err(|error| error.to_string())?,
            )
            .map_err(|error| error.to_string())?,
        )),
        payload,
    };
    state.validate()?;
    Ok(state)
}

pub fn restore(state: &StrategyRuntimeState) -> Result<Box<dyn Strategy>, String> {
    state.validate()?;
    macro_rules! decode {
        ($ty:ty) => {
            serde_json::from_slice::<$ty>(&state.payload)
                .map(|strategy| Box::new(strategy) as Box<dyn Strategy>)
                .map_err(|error| error.to_string())
        };
    }
    macro_rules! decode_native {
        ($ty:ty) => {{
            let strategy: $ty =
                serde_json::from_slice(&state.payload).map_err(|error| error.to_string())?;
            strategy.core.config.validate().map_err(str::to_string)?;
            strategy.core.state.validate().map_err(str::to_string)?;
            Ok(Box::new(strategy) as Box<dyn Strategy>)
        }};
    }
    let strategy = match state.kind.as_str() {
        crate::native_carry::plug::NAME => decode_native!(crate::native_carry::plug::NativeCarry),
        crate::native_long::plug::NAME => decode_native!(crate::native_long::plug::NativeLong),
        crate::native_exodus::plug::NAME => {
            decode_native!(crate::native_exodus::plug::NativeExodus)
        }
        crate::quoter::plug::NAME => decode!(crate::quoter::Quoter),
        crate::probe::NAME => decode!(crate::probe::Probe),
        kind => Err(format!("strategy process kind {kind:?} is not registered")),
    }?;
    let decoded = strategy
        .runtime_state()?
        .ok_or("restored strategy omitted its runtime contract")?;
    if decoded.kind != state.kind || decoded.configuration_sha256 != state.configuration_sha256 {
        return Err("strategy runtime payload disagrees with its registered configuration".into());
    }
    Ok(strategy)
}
