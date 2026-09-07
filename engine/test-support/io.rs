//! Keep Tokio's paused clock fixed while the kernel supplies local socket readiness.
//! Tests advance timers explicitly after observing the relevant I/O boundary.

pub struct IoProgress(tokio::task::JoinHandle<()>);

impl IoProgress {
    pub fn new() -> Self {
        Self(tokio::spawn(async {
            loop {
                tokio::task::yield_now().await;
            }
        }))
    }
}

impl Drop for IoProgress {
    fn drop(&mut self) {
        self.0.abort();
    }
}
