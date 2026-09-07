//! Shared private-stream bookkeeping; authentication and reset ordering stay with each venue.
use engine_types::{FeedError, OrderUpdate};
use std::collections::{HashSet, VecDeque};
use std::future::Future;
use std::time::Duration;
use tokio::sync::mpsc;

pub(crate) type Handover = Result<OrderUpdate, FeedError>;
pub(crate) struct Gone;

pub(crate) async fn until_closed<T>(tx: &mpsc::Sender<Handover>, work: impl Future<Output = T>) {
    tokio::select! { _ = tx.closed() => (), _ = work => () }
}
pub(crate) async fn hand_over(tx: &mpsc::Sender<Handover>, item: Handover) -> Result<(), Gone> {
    tx.send(item).await.map_err(|_| Gone)
}

#[derive(Default)]
pub(crate) struct ReconnectBackoff {
    delay: Duration,
}
impl ReconnectBackoff {
    fn next_delay(&mut self) -> Duration {
        let delay = self.delay;
        self.delay = if delay.is_zero() {
            Duration::from_millis(250)
        } else {
            (delay * 2).min(Duration::from_secs(30))
        };
        delay
    }
    pub(crate) async fn wait(&mut self) {
        let delay = self.delay;
        if !delay.is_zero() {
            tokio::time::sleep(delay).await;
        }
        self.next_delay();
    }
    pub(crate) fn completed_session(&mut self, elapsed: Duration) {
        if elapsed >= Duration::from_secs(30) {
            self.delay = Duration::ZERO;
        }
    }
}

pub(crate) struct AckMemory {
    ids: HashSet<String>,
    order: VecDeque<String>,
    capacity: usize,
}
impl AckMemory {
    pub(crate) fn new(capacity: usize) -> Self {
        Self {
            ids: HashSet::new(),
            order: VecDeque::new(),
            capacity,
        }
    }
    pub(crate) fn remember(&mut self, id: &str) -> bool {
        if !self.ids.insert(id.to_string()) {
            return false;
        }
        self.order.push_back(id.to_string());
        while self.order.len() > self.capacity {
            if let Some(old) = self.order.pop_front() {
                self.ids.remove(&old);
            }
        }
        true
    }
    #[cfg(test)]
    pub(crate) fn lengths(&self) -> (usize, usize) {
        (self.ids.len(), self.order.len())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn backoff_preserves_immediate_retry_growth_cap_and_healthy_reset() {
        let mut state = ReconnectBackoff::default();
        assert_eq!(state.next_delay(), Duration::ZERO);
        assert_eq!(state.next_delay(), Duration::from_millis(250));
        assert_eq!(state.next_delay(), Duration::from_millis(500));
        for _ in 0..20 {
            state.next_delay();
        }
        assert_eq!(state.next_delay(), Duration::from_secs(30));
        state.completed_session(Duration::from_millis(29999));
        assert_eq!(state.next_delay(), Duration::from_secs(30));
        state.completed_session(Duration::from_secs(30));
        assert_eq!(state.next_delay(), Duration::ZERO);
    }
    #[test]
    fn ack_eviction_is_fifo_and_duplicates_do_not_renew_retention() {
        let mut memory = AckMemory::new(2);
        assert!(memory.remember("a"));
        assert!(memory.remember("b"));
        assert!(!memory.remember("a"));
        assert!(memory.remember("c"));
        assert!(memory.remember("a"));
        assert_eq!(memory.lengths(), (2, 2));
    }
    #[tokio::test(start_paused = true)]
    async fn dropping_receiver_cancels_blocked_delivery_and_drops_the_session() {
        struct Dropped(std::sync::Arc<std::sync::atomic::AtomicBool>);
        impl Drop for Dropped {
            fn drop(&mut self) {
                self.0.store(true, std::sync::atomic::Ordering::SeqCst);
            }
        }
        let (tx, rx) = mpsc::channel(1);
        tx.send(Err(FeedError::Closed)).await.unwrap();
        let dropped = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let held = Dropped(dropped.clone());
        {
            let work = async {
                let _held = held;
                hand_over(&tx, Err(FeedError::Closed)).await
            };
            tokio::pin!(work);
            tokio::select! { biased; _=&mut work => panic!("queue must apply backpressure"), _=tokio::task::yield_now()=>() }
            drop(rx);
            until_closed(&tx, work).await;
        }
        assert!(dropped.load(std::sync::atomic::Ordering::SeqCst));
    }
}
