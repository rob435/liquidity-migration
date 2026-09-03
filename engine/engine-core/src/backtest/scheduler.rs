//! Virtual time for the replay: one clock, a set of waiters on it, and the
//! rule that decides who runs next.
//!
//! The clock only moves when [`Scheduler::advance_to`] is called, and the
//! tape feed is the only caller. Everything else that wants to wait — the
//! loop's flush tick, a strategy timer, the venue holding an order for its
//! round trip, the private stream holding a fill for its hop — registers a
//! deadline and is woken when the clock reaches it. A woken waiter is
//! **fired** until its future is polled; while any fired waiter is
//! unconsumed the feed will not release the next row, so nothing at a later
//! instant can be observed before something due at an earlier one.
//!
//! Dropping a waiter's future — which `select!` does to every branch that
//! did not win — removes it whether pending or fired. A dropped sleep can
//! therefore never hold time still.

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll, Waker};
use std::time::Duration;

use crate::engine::{LoopInterval, LoopTimer};

/// Who is waiting, so the tape's end can let the venue and the private
/// stream finish what is in flight without letting a periodic tick run
/// forever.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum WaiterKind {
    /// The loop's group-flush tick or a strategy timer.
    Timer,
    /// The venue holding a command for its modelled round trip.
    Venue,
    /// The private stream holding an update for its modelled hop.
    Private,
    /// A durable signal waiting for its availability instant.
    Signal,
}

#[derive(Default)]
struct Inner {
    now_ns: u64,
    /// Set when the tape is exhausted. From then on a wait completes on its
    /// first poll, moving the clock to its deadline, so the loop's settle
    /// and graceful stop run to completion in one burst instead of waiting
    /// for a clock nobody advances any more.
    closed: bool,
    /// Set once the tape feed has started releasing rows. Until then nothing
    /// can move the clock, so a wait completes at once without moving it:
    /// boot's venue reads happen at the tape's first instant.
    pumped: bool,
    /// How many times the feed has been polled: the idle pump's evidence
    /// that the loop is (not) running.
    feed_polls: u64,
    next_id: u64,
    /// Waiting for the clock, keyed so the earliest deadline is first.
    pending: BTreeMap<(u64, u64), Pending>,
    /// Woken by the clock and not yet polled to completion.
    fired: HashMap<u64, Option<Waker>>,
}

struct Pending {
    kind: WaiterKind,
    waker: Option<Waker>,
}

/// The replay's clock. Cheap to clone; every clone is the same clock.
#[derive(Clone, Default)]
pub struct Scheduler {
    inner: Arc<Mutex<Inner>>,
}

impl Scheduler {
    pub fn starting_at(now_ns: u64) -> Self {
        let scheduler = Scheduler::default();
        scheduler.lock().now_ns = now_ns;
        scheduler
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Inner> {
        self.inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    pub fn now_ns(&self) -> u64 {
        self.lock().now_ns
    }

    /// The tape feed is now pumping the clock. See `Inner::pumped`.
    pub fn open(&self) {
        self.lock().pumped = true;
    }

    /// The tape has ended. See `Inner::closed`.
    pub fn close(&self) {
        self.lock().closed = true;
    }

    pub fn is_closed(&self) -> bool {
        self.lock().closed
    }

    pub fn is_pumped(&self) -> bool {
        self.lock().pumped
    }

    pub fn note_feed_poll(&self) {
        self.lock().feed_polls += 1;
    }

    pub fn feed_polls(&self) -> u64 {
        self.lock().feed_polls
    }

    /// The earliest instant anything is waiting for, of the given kinds.
    pub fn earliest_pending(&self, kinds: &[WaiterKind]) -> Option<u64> {
        self.lock()
            .pending
            .iter()
            .find(|(_, pending)| kinds.contains(&pending.kind))
            .map(|((deadline, _), _)| *deadline)
    }

    /// Whether some waiter has been woken and not yet run. While true the
    /// clock must not move.
    pub fn has_fired_unconsumed(&self) -> bool {
        !self.lock().fired.is_empty()
    }

    /// Move the clock to `deadline_ns` (never backwards), waking everything
    /// due by then. The thread's virtual clock moves with it.
    pub fn advance_to(&self, deadline_ns: u64) {
        let mut inner = self.lock();
        if deadline_ns > inner.now_ns {
            inner.now_ns = deadline_ns;
        }
        let now = inner.now_ns;
        let mut due = Vec::new();
        while let Some(entry) = inner.pending.first_entry() {
            if entry.key().0 > now {
                break;
            }
            let ((_, id), pending) = entry.remove_entry();
            due.push((id, pending.waker));
        }
        for (id, waker) in due {
            if let Some(waker) = &waker {
                waker.wake_by_ref();
            }
            inner.fired.insert(id, waker);
        }
        drop(inner);
        // The install happens once, by the runner, before the loop boots;
        // a clock that is not installed is a driver bug and is reported as
        // one there, not here.
        let _ = engine_types::clock::advance_virtual_to(now);
    }

    /// A future that completes when the clock reaches `deadline_ns`.
    pub fn sleep_until(&self, deadline_ns: u64, kind: WaiterKind) -> VirtualSleep {
        let mut inner = self.lock();
        let id = inner.next_id;
        inner.next_id += 1;
        if deadline_ns > inner.now_ns {
            inner
                .pending
                .insert((deadline_ns, id), Pending { kind, waker: None });
        } else if inner.pumped && !inner.closed {
            // Already due. It still has to be seen: the feed must not
            // release a later row while this wait has not been polled, or a
            // branch that sits after the market branch in the loop's
            // `select!` — the flush tick, a strategy timer — would starve
            // behind a tape that is always ready.
            inner.fired.insert(id, None);
        }
        VirtualSleep {
            scheduler: self.clone(),
            id,
            deadline_ns,
            done: false,
        }
    }

    pub fn sleep(&self, duration: Duration, kind: WaiterKind) -> VirtualSleep {
        let deadline = self.now_ns().saturating_add(duration.as_nanos() as u64);
        self.sleep_until(deadline, kind)
    }
}

/// One wait on the virtual clock. Completes once the clock has reached its
/// deadline; removing itself on drop is what keeps a lost `select!` branch
/// from freezing time.
pub struct VirtualSleep {
    scheduler: Scheduler,
    id: u64,
    deadline_ns: u64,
    done: bool,
}

impl Future for VirtualSleep {
    type Output = ();

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        if self.done {
            return Poll::Ready(());
        }
        let mut inner = self.scheduler.lock();
        let fired = inner.fired.remove(&self.id).is_some();
        if fired || self.deadline_ns <= inner.now_ns {
            inner.pending.remove(&(self.deadline_ns, self.id));
            drop(inner);
            self.done = true;
            return Poll::Ready(());
        }
        if !inner.pumped {
            inner.pending.remove(&(self.deadline_ns, self.id));
            drop(inner);
            self.done = true;
            return Poll::Ready(());
        }
        if inner.closed {
            inner.pending.remove(&(self.deadline_ns, self.id));
            drop(inner);
            self.done = true;
            self.scheduler.advance_to(self.deadline_ns);
            return Poll::Ready(());
        }
        if let Some(pending) = inner.pending.get_mut(&(self.deadline_ns, self.id)) {
            pending.waker = Some(cx.waker().clone());
        }
        Poll::Pending
    }
}

impl Drop for VirtualSleep {
    fn drop(&mut self) {
        if self.done {
            return;
        }
        let mut inner = self.scheduler.lock();
        inner.pending.remove(&(self.deadline_ns, self.id));
        inner.fired.remove(&self.id);
    }
}

/// The loop's timers on the virtual clock.
#[derive(Clone)]
pub struct VirtualTimer {
    scheduler: Scheduler,
}

impl VirtualTimer {
    pub fn new(scheduler: Scheduler) -> Self {
        VirtualTimer { scheduler }
    }
}

impl LoopTimer for VirtualTimer {
    type Sleep = VirtualSleep;
    type Interval = VirtualInterval;

    fn sleep(&self, duration: Duration) -> Self::Sleep {
        self.scheduler.sleep(duration, WaiterKind::Timer)
    }

    fn interval(&self, period: Duration) -> Self::Interval {
        VirtualInterval {
            scheduler: self.scheduler.clone(),
            period_ns: period.as_nanos() as u64,
            // Tokio's interval ticks at once on its first poll.
            next_deadline_ns: self.scheduler.now_ns(),
        }
    }
}

/// A repeating virtual tick with `MissedTickBehavior::Delay` semantics: the
/// next tick is one period after the one that fired, however late it was.
pub struct VirtualInterval {
    scheduler: Scheduler,
    period_ns: u64,
    next_deadline_ns: u64,
}

impl LoopInterval for VirtualInterval {
    async fn tick(&mut self) {
        self.scheduler
            .sleep_until(self.next_deadline_ns, WaiterKind::Timer)
            .await;
        self.next_deadline_ns = self.scheduler.now_ns().saturating_add(self.period_ns);
    }
}

/// Return `Pending` once, asking to be polled again straight away. The tape
/// feed uses it to hand the loop back to whichever branch the clock just
/// woke, before releasing anything later.
pub struct YieldNow {
    yielded: bool,
}

impl YieldNow {
    pub fn new() -> Self {
        YieldNow { yielded: false }
    }
}

impl Default for YieldNow {
    fn default() -> Self {
        Self::new()
    }
}

impl Future for YieldNow {
    type Output = ();

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        if self.yielded {
            return Poll::Ready(());
        }
        self.yielded = true;
        cx.waker().wake_by_ref();
        Poll::Pending
    }
}
