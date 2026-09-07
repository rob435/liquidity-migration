use std::future::{poll_fn, Future};

/// Keep the engine's thread-local clock aligned with paused Tokio time at
/// each poll of the engine future; no process-global clock is changed.
pub async fn with_engine_clock<F: Future>(future: F) -> F::Output {
    let origin = engine_types::clock::mono_ns();
    let _clock = engine_types::clock::install_virtual(engine_types::clock::wall_ns(), origin)
        .expect("test already installed an engine clock");
    let started = tokio::time::Instant::now();
    tokio::pin!(future);
    poll_fn(|cx| {
        let elapsed = u64::try_from(started.elapsed().as_nanos()).expect("test clock overflow");
        engine_types::clock::advance_virtual_to(origin.saturating_add(elapsed)).unwrap();
        future.as_mut().poll(cx)
    })
    .await
}
