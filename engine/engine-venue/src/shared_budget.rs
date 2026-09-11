use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::time::Instant;

#[derive(Clone)]
pub(crate) struct SharedBudget(Arc<Mutex<State>>);
struct State {
    window: Duration,
    capacity: u32,
    next_id: u64,
    reservations: BTreeMap<u64, (u32, Option<Instant>)>,
}
pub(crate) struct Reservation {
    budget: SharedBudget,
    id: u64,
}

impl SharedBudget {
    pub(crate) fn new(window: Duration, capacity: u32) -> Self {
        Self(Arc::new(Mutex::new(State {
            window,
            capacity,
            next_id: 0,
            reservations: BTreeMap::new(),
        })))
    }
    fn try_reserve(&self, cost: u32, now: Instant) -> Result<Reservation, Duration> {
        let mut state = self.0.lock().expect("quota mutex poisoned");
        assert!(cost > 0 && cost <= state.capacity);
        let window = state.window;
        state.reservations.retain(|_, (_, completed)| {
            completed.is_none_or(|at| now.saturating_duration_since(at) < window)
        });
        let used: u32 = state.reservations.values().map(|(cost, _)| cost).sum();
        if used + cost <= state.capacity {
            let id = state.next_id;
            state.next_id = state
                .next_id
                .checked_add(1)
                .expect("quota reservation id exhausted");
            state.reservations.insert(id, (cost, None));
            return Ok(Reservation {
                budget: self.clone(),
                id,
            });
        }
        // Capacity frees at `completed_at + window`, so the earliest a slot
        // can open is that instant over the completed reservations; with none
        // completed, no reservation can free before `now + window`.
        Err(state
            .reservations
            .values()
            .filter_map(|(_, at)| at.map(|at| (at + window).saturating_duration_since(now)))
            .min()
            .unwrap_or(window))
    }
    #[cfg(feature = "binance")]
    pub(crate) async fn reserve(&self, cost: u32) -> Reservation {
        loop {
            match self.try_reserve(cost, Instant::now()) {
                Ok(reservation) => return reservation,
                Err(wait) => tokio::time::sleep(wait).await,
            }
        }
    }
}
impl Drop for Reservation {
    fn drop(&mut self) {
        let mut state = self.budget.0.lock().expect("quota mutex poisoned");
        if let Some((_, completed)) = state.reservations.get_mut(&self.id) {
            *completed = Some(Instant::now());
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn out_of_order_completion_keeps_each_cost_until_its_own_window_expires() {
        let budget = SharedBudget::new(Duration::from_secs(60), 3);
        let began = Instant::now();
        let slow = budget.try_reserve(2, began).ok().unwrap();
        let fast = budget.try_reserve(1, began).ok().unwrap();
        drop(fast);
        assert!(budget
            .try_reserve(2, began + Duration::from_secs(61))
            .is_err());
        let next = budget
            .try_reserve(1, began + Duration::from_secs(61))
            .ok()
            .unwrap();
        assert!(budget
            .try_reserve(1, began + Duration::from_secs(120))
            .is_err());
        drop(slow);
        assert_eq!(
            budget
                .0
                .lock()
                .unwrap()
                .reservations
                .get(&next.id)
                .unwrap()
                .1,
            None
        );
        drop(next);
        assert!(budget
            .try_reserve(3, Instant::now() + Duration::from_secs(61))
            .is_ok());
    }

    #[test]
    fn a_refusal_waits_until_capacity_can_free_and_no_longer() {
        let window = Duration::from_secs(60);
        let budget = SharedBudget::new(window, 1);
        let began = Instant::now();
        let held = budget.try_reserve(1, began).ok().unwrap();
        // Still in flight: it cannot complete before `now`, so it cannot free
        // before `now + window`.
        assert_eq!(budget.try_reserve(1, began).err(), Some(window));
        let id = held.id;
        drop(held);
        let completed = budget.0.lock().unwrap().reservations[&id]
            .1
            .expect("dropping a reservation stamps its completion");
        let waited = Duration::from_secs(20);
        assert_eq!(
            budget.try_reserve(1, completed + waited).err(),
            Some(window - waited)
        );
    }
}
