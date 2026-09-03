//! The engine's one monotonic clock.
//!
//! Every `*_ns` stamp in events, orders, the log, and the latency ledger
//! comes from here, so stamps taken in different crates are comparable.
//! The origin is the first call in the process; these are not wall times.
//!
//! A replay driver may install a **virtual** clock on its own thread. The
//! override is thread-local: the current-thread runtime the engine loop and
//! its venue task share sees it, and no other thread in the process — a test
//! running beside it, the live engine — can. It is held by an RAII guard, so
//! a driver that fails part-way cannot leave the thread in the past.

use std::cell::Cell;
use std::sync::OnceLock;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

fn origin() -> Instant {
    static ORIGIN: OnceLock<Instant> = OnceLock::new();
    *ORIGIN.get_or_init(Instant::now)
}

/// One virtual instant. `mono_ns` is the single source of truth; the wall
/// stamps are derived from it, so the three readings can never disagree or
/// move against each other.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Virtual {
    origin_mono_ns: u64,
    origin_wall_ns: u64,
    mono_ns: u64,
}

impl Virtual {
    fn wall_ns(self) -> u64 {
        self.origin_wall_ns
            .saturating_add(self.mono_ns.saturating_sub(self.origin_mono_ns))
    }
}

thread_local! {
    static VIRTUAL: Cell<Option<Virtual>> = const { Cell::new(None) };
}

/// Holds the virtual clock installed on this thread; dropping it restores
/// the system clocks. Not `Send`: the clock it guards is this thread's.
#[derive(Debug)]
pub struct VirtualClockGuard {
    _not_send: std::marker::PhantomData<*const ()>,
}

impl Drop for VirtualClockGuard {
    fn drop(&mut self) {
        VIRTUAL.with(|cell| cell.set(None));
    }
}

/// Put this thread on a virtual clock reading `wall_ns` at monotonic
/// `mono_ns`. Refused while one is already installed: two drivers on one
/// thread would each believe they own time.
pub fn install_virtual(wall_ns: u64, mono_ns: u64) -> Result<VirtualClockGuard, VirtualClockError> {
    VIRTUAL.with(|cell| {
        if cell.get().is_some() {
            return Err(VirtualClockError::AlreadyInstalled);
        }
        cell.set(Some(Virtual {
            origin_mono_ns: mono_ns,
            origin_wall_ns: wall_ns,
            mono_ns,
        }));
        Ok(VirtualClockGuard {
            _not_send: std::marker::PhantomData,
        })
    })
}

/// Move this thread's virtual clock forward to `mono_ns`. Never backwards:
/// a stamp earlier than the clock already reads leaves it where it is, so
/// an out-of-order row on a tape cannot make a duration negative.
pub fn advance_virtual_to(mono_ns: u64) -> Result<(), VirtualClockError> {
    VIRTUAL.with(|cell| match cell.get() {
        Some(mut v) => {
            if mono_ns > v.mono_ns {
                v.mono_ns = mono_ns;
                cell.set(Some(v));
            }
            Ok(())
        }
        None => Err(VirtualClockError::NotInstalled),
    })
}

/// Whether this thread reads a virtual clock.
pub fn is_virtual() -> bool {
    VIRTUAL.with(|cell| cell.get().is_some())
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, thiserror::Error)]
pub enum VirtualClockError {
    #[error("a virtual clock is already installed on this thread")]
    AlreadyInstalled,
    #[error("no virtual clock is installed on this thread")]
    NotInstalled,
}

/// Engine monotonic nanoseconds since the process's clock origin.
pub fn mono_ns() -> u64 {
    match VIRTUAL.with(Cell::get) {
        Some(v) => v.mono_ns,
        None => origin().elapsed().as_nanos() as u64,
    }
}

/// Wall-clock milliseconds since the unix epoch, for venue timestamps and
/// human-readable records.
pub fn wall_ms() -> i64 {
    match VIRTUAL.with(Cell::get) {
        Some(v) => (v.wall_ns() / 1_000_000) as i64,
        None => SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0),
    }
}

/// Wall-clock nanoseconds since the unix epoch. This is the bridge between
/// monotonic engine timings and externally captured venue data; it is not
/// used to measure durations.
pub fn wall_ns() -> u64 {
    match VIRTUAL.with(Cell::get) {
        Some(v) => v.wall_ns(),
        None => SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stamps_share_one_origin_and_never_go_backwards() {
        let a = mono_ns();
        let b = mono_ns();
        assert!(b >= a);
    }

    #[test]
    fn wall_stamps_share_the_unix_epoch() {
        let ms = wall_ms().max(0) as u64;
        let ns = wall_ns();
        assert!(ns / 1_000_000 >= ms.saturating_sub(1));
        assert!(ns / 1_000_000 <= ms.saturating_add(1));
    }

    #[test]
    fn a_virtual_clock_reads_what_it_was_given_and_advances_together() {
        let guard = install_virtual(1_700_000_000_000_000_000, 10_000_000).unwrap();
        assert!(is_virtual());
        assert_eq!(mono_ns(), 10_000_000);
        assert_eq!(wall_ns(), 1_700_000_000_000_000_000);
        assert_eq!(wall_ms(), 1_700_000_000_000);

        advance_virtual_to(5_010_000_000).unwrap();
        assert_eq!(mono_ns(), 5_010_000_000);
        assert_eq!(wall_ns(), 1_700_000_005_000_000_000);
        assert_eq!(wall_ms(), 1_700_000_005_000);
        drop(guard);
        assert!(!is_virtual());
    }

    #[test]
    fn a_virtual_clock_never_moves_backwards() {
        let _guard = install_virtual(1_000_000_000_000, 1_000).unwrap();
        advance_virtual_to(5_000).unwrap();
        advance_virtual_to(2_000).unwrap();
        assert_eq!(mono_ns(), 5_000);
    }

    #[test]
    fn the_override_is_confined_to_its_own_thread() {
        let _guard = install_virtual(1_000_000_000_000, 7).unwrap();
        assert_eq!(mono_ns(), 7);
        let elsewhere = std::thread::spawn(|| (is_virtual(), mono_ns() > 7))
            .join()
            .unwrap();
        assert_eq!(
            elsewhere,
            (false, true),
            "another thread keeps the system clock"
        );
    }

    #[test]
    fn a_second_installation_is_refused_and_the_guard_releases() {
        let guard = install_virtual(1, 1).unwrap();
        assert_eq!(
            install_virtual(2, 2).map(|_| ()),
            Err(VirtualClockError::AlreadyInstalled)
        );
        drop(guard);
        assert_eq!(advance_virtual_to(3), Err(VirtualClockError::NotInstalled));
        let _again = install_virtual(2, 2).unwrap();
        assert_eq!(mono_ns(), 2);
    }
}
