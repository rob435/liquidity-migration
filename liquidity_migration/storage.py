from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import polars as pl


# Per-process thread-lock per dataset path. The file-based lock that follows
# only serializes ACROSS processes; within a single process, multiple worker
# threads contending on the file lock can wedge because they all write the
# same pid into the lock file and then read it back as "my own pid -> still
# alive somewhere -> keep waiting" even when the actual holder has silently
# dropped the file via an unlink race. This per-process lock ensures only
# one thread of this process ever enters the file-lock acquire/release dance.
_DATASET_THREAD_LOCKS: dict[str, threading.Lock] = {}
_DATASET_THREAD_LOCKS_GUARD = threading.Lock()
_DATASET_TMP_SWEEP_LAST: dict[str, float] = {}
_DATASET_TMP_SWEEP_GUARD = threading.Lock()


# storage-concurrency-2: per-acquisition tokens currently OWNED by THIS live
# process. _lock_owner_is_dead short-circuits a pid==os.getpid() payload to
# "alive" so a process never evicts its own live lock. But a singleton lock
# taken with stale_seconds=0 (event_ws_risk_cycle.lock) has no age-eviction
# backstop: if the daemon crashes holding the lock and systemd restarts it
# within RestartSec and the kernel hands the SAME pid back (Linux pids cycle to
# pid_max), the new process reads its dead predecessor's pid==getpid() and would
# block forever. The token written into every payload (line ~286) is the
# tiebreaker: a token NOT in this set is from a previous incarnation that merely
# reused our pid, so the owner is genuinely dead and the lock is evictable.
_LIVE_OWNED_TOKENS: set[str] = set()
_LIVE_OWNED_TOKENS_GUARD = threading.Lock()


def _register_owned_token(token: str) -> None:
    with _LIVE_OWNED_TOKENS_GUARD:
        _LIVE_OWNED_TOKENS.add(token)


def _unregister_owned_token(token: str) -> None:
    with _LIVE_OWNED_TOKENS_GUARD:
        _LIVE_OWNED_TOKENS.discard(token)


def _token_is_live_owned(token: str | None) -> bool:
    if not token:
        return False
    with _LIVE_OWNED_TOKENS_GUARD:
        return token in _LIVE_OWNED_TOKENS


def _thread_lock_for(lock_path: Path) -> threading.Lock:
    key = str(lock_path.resolve())
    with _DATASET_THREAD_LOCKS_GUARD:
        lock = _DATASET_THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _DATASET_THREAD_LOCKS[key] = lock
        return lock


def _unlink_with_retry(lock_path: Path, *, retries: int = 40, delay: float = 0.05) -> None:
    """Unlink a lock file with retries on Windows PermissionError (WinError 32).

    Windows raises ``PermissionError: [WinError 32] The process cannot
    access the file because it is being used by another process`` whenever
    ANY process holds the file open — including the brief windows when
    another process is mid-``_read_lock_text_safe`` reading our payload to
    check liveness. With many parallel sweep workers all contending on the
    same dataset lock, those windows overlap with the lock holder's release
    and naively-`.unlink()`-only-catching-FileNotFoundError crashes the
    whole subprocess (and propagates failure to the orchestrator).

    The retry loop tolerates this transient contention. If retries exhaust,
    the file is left in place — the next acquire's stale-detection (dead
    pid via _lock_owner_is_dead) will clean it up, so no permanent leak.
    Defaults: 40 retries × 50ms ≈ 2s, double the 1s ``_read_lock_text_safe``
    timeout the contending readers wait."""
    for _ in range(retries):
        try:
            lock_path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(delay)
    # Last resort: leave the file. The next acquire's stale-detection
    # path is the safety net.


DATASETS = {
    "instruments",
    "klines_1m",
    "klines_1h",
    "klines_5m",
    "funding",
    "open_interest",
    "mark_price_1h",
    "index_price_1h",
    "premium_index_1h",
    # Read-only legacy: the download path was deleted with the signed_flow
    # cleanup (validated as not-an-edge, commit 6e5e977). Kept ONLY so old
    # research roots that still hold a signed_flow_1h directory stay readable
    # by ad-hoc tooling.
    "signed_flow_1h",
    "ticker_snapshots",
    "archive_trade_manifest",
    "universe_current",
    "event_demo_klines_1h",
    "event_demo_trades",
    "event_demo_orders",
    "event_demo_cycles",
    "long_native_demo_trades",
    "long_native_demo_orders",
    "long_native_demo_cycles",
    # B.4: paper-shadow ledger for the long sleeve. Same schema as the demo
    # ledger; written by the paper runner which records idealised fills at the
    # signal price and never submits orders.
    "long_native_paper_trades",
    "long_native_paper_orders",
    "long_native_paper_cycles",
    # Continuous-fade sleeve (SHORT-direction): own ledger root,
    # own datasets so it nets/reconciles separately from compatibility + long on the
    # shared demo account. demo = live-submitted; paper = idealised-fill shadow.
    "continuous_fade_demo_trades",
    "continuous_fade_demo_orders",
    "continuous_fade_demo_cycles",
    "continuous_fade_paper_trades",
    "continuous_fade_paper_orders",
    "continuous_fade_paper_cycles",
    "binance_usdm_klines_1h",
    "binance_usdm_mark_price_1h",
    "binance_usdm_index_price_1h",
    "binance_usdm_premium_index_1h",
    "binance_usdm_funding",
    "binance_usdm_open_interest",
    "binance_usdm_taker_flow_1h",
    # Cross-sleeve margin-budget + same-symbol reservation control table
    # (long-sleeve-5/6). ONE control row per netted account, OWNED + rewritten by
    # ws_risk each reconcile pass; every sleeve reads it read-only before sizing.
    # Structured fields are JSON-string columns (scalar Utf8 is schema-stable under
    # the diagonal_relaxed concat/dedup the read+write paths use). See
    # read_control_row / write_control_row below.
    "cross_sleeve_account_state",
}

DATASET_KEYS = {
    "instruments": ("symbol",),
    "klines_1m": ("ts_ms", "symbol"),
    "klines_1h": ("ts_ms", "symbol"),
    "klines_5m": ("ts_ms", "symbol"),
    "funding": ("ts_ms", "symbol"),
    "open_interest": ("ts_ms", "symbol"),
    "mark_price_1h": ("ts_ms", "symbol"),
    "index_price_1h": ("ts_ms", "symbol"),
    "premium_index_1h": ("ts_ms", "symbol"),
    "signed_flow_1h": ("ts_ms", "symbol"),
    "ticker_snapshots": ("ts_ms", "symbol"),
    "archive_trade_manifest": ("symbol", "date", "url"),
    "universe_current": ("snapshot_ts_ms", "symbol"),
    "event_demo_klines_1h": ("ts_ms", "symbol"),
    "event_demo_trades": ("trade_id",),
    "event_demo_orders": ("order_link_id",),
    "event_demo_cycles": ("cycle_id",),
    "long_native_demo_trades": ("trade_id",),
    "long_native_demo_orders": ("order_link_id",),
    "long_native_demo_cycles": ("cycle_id",),
    "long_native_paper_trades": ("trade_id",),
    "long_native_paper_orders": ("order_link_id",),
    "long_native_paper_cycles": ("cycle_id",),
    "continuous_fade_demo_trades": ("trade_id",),
    "continuous_fade_demo_orders": ("order_link_id",),
    "continuous_fade_demo_cycles": ("cycle_id",),
    "continuous_fade_paper_trades": ("trade_id",),
    "continuous_fade_paper_orders": ("order_link_id",),
    "continuous_fade_paper_cycles": ("cycle_id",),
    "binance_usdm_klines_1h": ("ts_ms", "symbol"),
    "binance_usdm_mark_price_1h": ("ts_ms", "symbol"),
    "binance_usdm_index_price_1h": ("ts_ms", "symbol"),
    "binance_usdm_premium_index_1h": ("ts_ms", "symbol"),
    "binance_usdm_funding": ("ts_ms", "symbol"),
    "binance_usdm_open_interest": ("ts_ms", "symbol"),
    "binance_usdm_taker_flow_1h": ("ts_ms", "symbol"),
    # Single control row per netted account; key = account_key (write_control_row
    # relies on this so _write_part dedups the upsert on it).
    "cross_sleeve_account_state": ("account_key",),
}


# Canonical-name fallbacks: when a root stores a dataset under a venue-specific
# name instead of the canonical one, reads of the canonical name transparently
# resolve to whichever venue variant is actually present. This is why a raw
# per-venue root (e.g. binance_full_pit, which stores funding as
# binance_usdm_funding) is funding-modeled with NO symlink/rename — read_dataset
# (root, "funding") finds binance_usdm_funding on its own. Extend per venue.
_DATASET_FALLBACKS: dict[str, tuple[str, ...]] = {
    "funding": ("binance_usdm_funding",),
    "open_interest": ("binance_usdm_open_interest",),
}


# pit-data-6 is now enforced by canonical-precedence in resolve_dataset_name (a real
# Bybit root always carries its own canonical funding/ dir, which wins over any
# binance_usdm_* proxy on the same root) rather than a klines-name marker — the marker
# false-positived on Binance full-PIT roots that store klines under the canonical name.

def resolve_dataset_name(data_root: str | Path, dataset: str) -> str:
    """Map a canonical dataset request to the variant actually present in ``root``.

    Returns ``dataset`` unchanged when the canonical directory exists (or when no
    fallback applies); otherwise the first known venue-variant that exists on
    disk. The returned name is always a member of :data:`DATASETS`, so the lock
    and path helpers stay valid.

    pit-data-6: a missing canonical funding/OI dir must never silently resolve to
    the WRONG venue's modeled cost curve. The safety invariant used here is that a
    ``binance_usdm_*`` variant dir only ever exists on a Binance root, so a present
    variant is authoritative and a Bybit root (which never carries one) falls back
    to the canonical (empty) name -> funding_mode=missing.
    """
    fallbacks = _DATASET_FALLBACKS.get(dataset)
    if not fallbacks:
        return dataset
    root = Path(data_root).expanduser()
    if (root / dataset).exists():
        return dataset
    # A present venue-variant dataset is AUTHORITATIVE: a ``binance_usdm_*`` dir only
    # ever exists on a Binance root, so prefer it even when the root stores klines
    # under the canonical ``klines_1h/`` name. The old code suppressed this whenever
    # a Bybit-native kline marker was present, which false-positived on Binance
    # full-PIT roots that use canonical kline naming -> funding/OI silently uncosted
    # (the 2026-06-15 resolver regression: funding_mode=missing on a fully populated
    # binance_usdm_funding).
    for alt in fallbacks:
        if (root / alt).exists():
            return alt
    # No variant present. A Bybit root whose funding/OI dir is simply absent returns
    # the canonical (empty) name -> funding_mode=missing, never substituting a
    # wrong-venue cost curve (pit-data-6 safety preserved: a Bybit root never carries
    # a binance_usdm_* dir, so the loop above cannot mis-substitute).
    return dataset


def dataset_path(data_root: str | Path, dataset: str) -> Path:
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")
    return Path(data_root).expanduser() / dataset


def dataset_lock_path(data_root: str | Path, dataset: str) -> Path:
    if dataset not in DATASETS:
        raise ValueError(f"Unknown dataset: {dataset}")
    return Path(data_root).expanduser() / ".locks" / f"{dataset}.lock"


def ensure_data_root(data_root: str | Path) -> Path:
    root = Path(data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def exclusive_file_lock(
    path: str | Path,
    *,
    stale_seconds: float = 600,
    poll_seconds: float = 0.05,
    invalid_lock_stale_seconds: float = 30.0,
) -> Iterator[None]:
    lock_path = Path(path).expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # Acquire the per-process thread-lock FIRST so only one thread of this
    # process can be in the file-lock body at a time. The file-lock below
    # then only serializes across processes, which is its real job and what
    # it actually handles correctly.
    with _thread_lock_for(lock_path):
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # POSIX-style "file already exists" — fall through to the
                # liveness / staleness / wait logic below.
                pass
            except PermissionError:
                # Windows-specific: when another process is mid-unlink the
                # file enters "delete-pending" state. A concurrent O_CREAT|O_EXCL
                # then raises PermissionError [Errno 13] / EACCES instead of
                # FileExistsError, even though the semantically-correct answer
                # is "the file exists, try again". Treat exactly like
                # FileExistsError so the same recovery / wait path runs.
                # Observed in Phase 0 dispatch: control cell crashed on lock
                # acquire of funding.lock while another worker was releasing it.
                pass
            else:
                # Got the lock fresh — break out of the wait loop.
                break
            if _lock_owner_is_dead(lock_path):
                _unlink_with_retry(lock_path)
                continue
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            invalid_lock_stale = (
                _lock_payload_is_invalid(lock_path)
                and invalid_lock_stale_seconds >= 0
                and age > invalid_lock_stale_seconds
            )
            if invalid_lock_stale:
                _unlink_with_retry(lock_path)
                continue
            if stale_seconds > 0 and age > stale_seconds:
                _unlink_with_retry(lock_path)
                continue
            time.sleep(max(poll_seconds, 0.0))
        # Capture the inode of the file WE created (fd is still open here, before
        # os.fdopen consumes it) so release only unlinks OUR lock. If our lock is
        # stale-evicted mid-critical-section and a successor recreates the path with a
        # new inode, an unconditional unlink-by-path would delete the SUCCESSOR's lock
        # and admit a second concurrent writer (CROS-1).
        try:
            _owned = os.fstat(fd)
            owned_key: tuple[int, int] | None = (_owned.st_dev, _owned.st_ino)
        except OSError:
            owned_key = None  # can't fstat -> fall back to legacy unlink-by-path
        # CROS-1b (audit 2026-06-09): (dev, ino) equality is NOT proof the path is
        # still OUR lock — ext4/overlayfs can hand a freed inode straight to a
        # successor's lock file created at the same path. A per-acquisition token
        # in the payload is the tiebreaker the inode check can't provide.
        owned_token = os.urandom(16).hex()
        # Register the token as live-owned BEFORE writing the payload: once the
        # bytes are on disk a concurrent _lock_owner_is_dead read could observe
        # our pid+token, and it must find the token live (not a reused-pid
        # ghost). Registering first closes that window.
        _register_owned_token(owned_token)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "created": time.time(), "token": owned_token}))
            yield
        finally:
            _unregister_owned_token(owned_token)
            if owned_key is None:
                _unlink_with_retry(lock_path)  # legacy path (fstat failed)
            else:
                try:
                    cur = os.stat(lock_path)
                    release_ours = (cur.st_dev, cur.st_ino) == owned_key
                except FileNotFoundError:
                    release_ours = False  # already gone -> nothing to unlink
                except OSError:
                    release_ours = True  # can't inode-check (e.g. Windows delete-pending) -> preserve legacy self-heal
                if release_ours:
                    # Inode matched — confirm the payload token before unlinking
                    # (inode-reuse defense, CROS-1b). An unreadable file keeps
                    # release_ours=True: failing to remove our own lock would
                    # strand waiters until stale eviction.
                    text = _read_lock_text_safe(lock_path)
                    if text is not None:
                        try:
                            release_ours = json.loads(text).get("token") == owned_token
                        except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                            release_ours = False  # foreign/corrupt payload -> not ours
                if release_ours:
                    _unlink_with_retry(lock_path)


def _read_lock_text_safe(lock_path: Path, timeout: float = 1.0) -> str | None:
    """Read the lock file text with a hard timeout. Returns None if the read
    blocks (e.g. Windows 'delete-pending' state where another thread of this
    same process unlinked the file but a handle keeps it half-alive — the
    naive Path.read_text() hangs in Path.open() forever). The outer lock
    loop treats None as 'unreadable, assume stale' and self-heals."""
    import threading
    box: list[str | None] = [None]
    def _read() -> None:
        try:
            box[0] = lock_path.read_text(encoding="utf-8")
        except Exception:
            pass
    t = threading.Thread(target=_read, daemon=True)
    t.start()
    t.join(timeout)
    return box[0]


def _pid_started_after(pid: int, created_ts: float) -> bool | None:
    """True if the live process ``pid`` started strictly AFTER ``created_ts`` (epoch
    seconds) — i.e. the lock's pid was REUSED by a newer process, so the original
    lock owner is actually dead. None when the start time can't be determined
    (non-Linux / no /proc), so the caller stays conservative and does NOT evict on a
    pid that os.kill reports as live. Linux-only via /proc/<pid>/stat; never raises
    (CROS-2: os.kill(pid,0) alone false-positives "alive" on a reused pid)."""
    if created_ts <= 0.0:
        return None
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            # comm (field 2) may contain spaces/parens; split on the LAST ") " so the
            # remaining whitespace-split fields start at field 3 (state).
            after_comm = fh.read().rsplit(") ", 1)[1].split()
        starttime_ticks = float(after_comm[19])  # field 22 (starttime), 0-indexed from field 3
        btime = 0.0
        with open("/proc/stat", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("btime "):
                    btime = float(line.split()[1])
                    break
        if btime <= 0.0:
            return None
        clk = os.sysconf("SC_CLK_TCK")
        if clk <= 0:
            return None
        proc_start_epoch = btime + starttime_ticks / clk
        return proc_start_epoch > created_ts + 1.0  # 1s slack vs clock granularity
    except Exception:  # noqa: BLE001 - any /proc parsing failure -> "unknown", stay conservative
        return None


def _lock_owner_is_dead(lock_path: Path) -> bool:
    text = _read_lock_text_safe(lock_path)
    if text is None:
        # File missing OR read hung (Windows delete-pending). Either way it
        # is safe to treat the owner as dead — the next iteration's unlink
        # is a no-op when the file is gone, and breaks delete-pending stalls
        # when it isn't.
        return True
    try:
        payload = json.loads(text)
        pid = int(payload.get("pid") or 0)
        created_ts = float(payload.get("created") or 0.0)
        token = payload.get("token")
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        # Normally a lock bearing our own pid is our own LIVE lock -> not dead.
        # But after a crash+fast-restart the kernel can hand the same pid to the
        # successor (Linux pids cycle to pid_max). The successor would then read
        # its dead predecessor's pid==getpid() and, with no age backstop on a
        # stale_seconds=0 singleton lock, block forever (storage-concurrency-2).
        # The per-acquisition token disambiguates: if the payload's token is one
        # WE currently own it is genuinely our live lock; otherwise it is a
        # predecessor that merely reused our pid and is dead -> evictable. A
        # legacy payload with no token is conservatively treated as our own live
        # lock (token is None -> not live-owned only when a token field exists).
        if token is None:
            return False
        return not _token_is_live_owned(token)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Foreign live pid — but it could be a REUSED pid whose original (dead) owner
        # held this lock. If the live pid started after the lock was created, evict.
        return _pid_started_after(pid, created_ts) is True
    except OverflowError:
        return True
    except OSError:
        # Windows: os.kill(pid, 0) on a non-signalable pid raises a bare OSError
        # whose winerror varies by pid value / Python build — 87
        # ERROR_INVALID_PARAMETER for an unknown pid, 11 ERROR_BAD_FORMAT for an
        # out-of-range pid (observed on Python 3.13 / Windows for pid
        # 2_147_483_647). POSIX dead pids raise ProcessLookupError and
        # live-but-foreign pids raise PermissionError — both handled above. ANY
        # other OSError here means the pid is not a signalable live process, so
        # treat the owner as dead; otherwise stale-lock recovery never fires and
        # every read/write_dataset blocks until the 6h stale timeout (the
        # sweep-hang we keep hitting). Safe on POSIX too: a live *owned* pid makes
        # os.kill succeed (no exception), so it is never misclassified as dead.
        return True
    # os.kill succeeded -> a live, signalable process holds this pid. It may be a
    # REUSED pid (the original owner was killed without cleanup); if the live process
    # started after the lock was created, the real owner is dead -> evict (CROS-2).
    return _pid_started_after(pid, created_ts) is True


def _lock_payload_is_invalid(lock_path: Path) -> bool:
    text = _read_lock_text_safe(lock_path)
    if text is None:
        # File missing or read hung — treat as invalid so the outer loop
        # can unlink and retry (self-heals Windows delete-pending stalls).
        return True
    try:
        payload = json.loads(text)
        pid = int(payload.get("pid") or 0)
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return True
    return pid <= 0


def with_date_column(df: pl.DataFrame, ts_col: str = "ts_ms") -> pl.DataFrame:
    if "date" in df.columns:
        return df
    return df.with_columns(
        pl.from_epoch(pl.col(ts_col), time_unit="ms")
        .dt.strftime("%Y-%m-%d")
        .alias("date")
    )


# reconcile-ledger-5 / quality-dup-5: the demo/paper trade+order ledgers were
# written with partition_by=() -> a single monolithic part.parquet that the live
# hot path read-modify-rewrites under the lock (write cost O(history)) and that
# ws_risk re-reads in full every reconcile pass (read cost O(history)). Bucket
# these ledgers by calendar month so each write/read touches only the current
# month. The bucket column MUST be derived from a per-row IMMUTABLE timestamp:
# dedup in _write_part is per-part-file, so a row that changes buckets across an
# update leaves a stale copy in the old bucket (a phantom OPEN trade on the
# netted account). For TRADE ledgers the stable source is entry_ts_ms (the exit
# reconcile path rewrites ts_ms=now_ms but preserves entry_ts_ms); for ORDER
# ledgers it is ts_ms (set once at creation, preserved on in-place update).
_LEDGER_MONTH_COL = "_ledger_month"
LEDGER_BUCKET_SOURCE: dict[str, str] = {
    "event_demo_trades": "entry_ts_ms",
    "event_demo_orders": "ts_ms",
    "long_native_demo_trades": "entry_ts_ms",
    "long_native_demo_orders": "ts_ms",
    "long_native_paper_trades": "entry_ts_ms",
    "long_native_paper_orders": "ts_ms",
    "continuous_fade_demo_trades": "entry_ts_ms",
    "continuous_fade_demo_orders": "ts_ms",
    "continuous_fade_paper_trades": "entry_ts_ms",
    "continuous_fade_paper_orders": "ts_ms",
    # Cycle heartbeat rows (~1440/day, wide payload, written every ~60s on the
    # order-submitting cycle path while holding the dataset lock): without a
    # bucket the per-cycle write read-concat-rewrote the WHOLE history monolith
    # — the same unbounded-growth class reconcile-ledger-5 fixed for trades/
    # orders (round 4). Cycle rows are append-once (cycle_id-keyed, never
    # updated), so ts_ms is immutable and bucket-stable.
    "continuous_fade_demo_cycles": "ts_ms",
    "continuous_fade_paper_cycles": "ts_ms",
}


def _with_ledger_month(df: pl.DataFrame, dataset: str) -> pl.DataFrame:
    """Add the int yyyymm _ledger_month partition column for a bucketed ledger
    dataset, derived from its registered IMMUTABLE timestamp source. Rows whose
    source ts is missing/null/<=0 fall into bucket 0 (legacy/unknown) so a
    malformed row never crashes the write. A no-op for datasets not in
    LEDGER_BUCKET_SOURCE or when the source column is absent."""
    src = LEDGER_BUCKET_SOURCE.get(dataset)
    if src is None or src not in df.columns or df.is_empty():
        return df
    month = pl.col(src).cast(pl.Int64, strict=False)
    return df.with_columns(
        pl.when(month.is_null() | (month <= 0))
        .then(pl.lit(0, dtype=pl.Int64))
        .otherwise(
            pl.from_epoch(month, time_unit="ms").dt.strftime("%Y%m").cast(pl.Int64, strict=False)
        )
        .fill_null(0)
        .alias(_LEDGER_MONTH_COL)
    )


def _recent_ledger_month_dirs(path: Path, months_back: int) -> list[Path]:
    """The most-recent ``months_back`` _ledger_month=* bucket dirs plus the
    legacy bucket (_ledger_month=0) -- the open-trade tail not yet migrated.
    Used by the windowed reconcile read."""
    dirs = [p for p in path.glob(f"{_LEDGER_MONTH_COL}=*") if p.is_dir()]

    def _month_of(p: Path) -> int:
        try:
            return int(p.name.split("=", 1)[1])
        except (IndexError, ValueError):
            return 0

    nonzero = sorted((p for p in dirs if _month_of(p) > 0), key=_month_of)
    recent = nonzero[-months_back:] if months_back > 0 else nonzero
    legacy = [p for p in dirs if _month_of(p) == 0]
    return recent + legacy


# storage-concurrency-4: how stale an orphaned `.*.tmp` part file must be before
# the sweep removes it. The temp file only exists for the brief window between
# write_parquet and the atomic rename in _write_part; any `.tmp` older than this
# is from a process that was SIGKILLed (OOM / TimeoutStopSec->SIGKILL / hard
# crash) mid-write, whose `finally: temp_path.unlink()` never ran. Generous so a
# slow in-flight write on another (impossible here — we hold the dataset lock)
# path is never clobbered.
_STALE_TMP_SECONDS = 600.0
_TMP_SWEEP_INTERVAL_SECONDS = 600.0


def _sweep_orphaned_tmp_parts(
    path: Path,
    *,
    stale_seconds: float = _STALE_TMP_SECONDS,
    recursive: bool = True,
) -> None:
    """Remove orphaned `.*.tmp` part files left by a crash between
    ``write_parquet`` and the atomic rename in :func:`_write_part`.

    _write_part writes to ``.{name}.{pid}.{ns}.tmp`` then renames; a SIGKILL in
    that window orphans the temp file because the ``finally`` unlink never runs.
    Readers never consume them (``glob('**/*.parquet')`` cannot match a leading-
    dot `.tmp` name), so this is a pure resource leak — but a long-lived
    Restart=always daemon that occasionally crashes accumulates them until the
    disk fills, which then fails the very ledger writes that record live orders.

    MUST be called while holding the dataset lock (every caller is inside
    :func:`write_dataset`'s ``exclusive_file_lock``), so there is no live writer
    whose in-flight temp could be deleted. The age gate is a second belt: only
    temp files older than ``stale_seconds`` are removed."""
    if not path.exists():
        return
    now = time.time()
    pattern = "**/.*.tmp" if recursive else ".*.tmp"
    for tmp in path.glob(pattern):
        try:
            if now - tmp.stat().st_mtime <= stale_seconds:
                continue
            tmp.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            # Best-effort cleanup: a transient stat/unlink failure (e.g. Windows
            # delete-pending) must never break the write that triggered the sweep.
            continue


def _sweep_orphaned_tmp_parts_if_due(
    path: Path,
    *,
    interval_seconds: float = _TMP_SWEEP_INTERVAL_SECONDS,
) -> None:
    """Throttle the expensive full-tree temp sweep per process and dataset.

    Full-PIT datasets can contain hundreds of thousands of partition files. A
    recursive glob before every tiny partition write turns a top-up into
    O(writes * whole-tree walk). _write_part still sweeps the target directory
    on every write; this preserves the broad cleanup safety net without putting
    it on every partition.
    """
    key = str(path.resolve())
    now = time.time()
    with _DATASET_TMP_SWEEP_GUARD:
        last = _DATASET_TMP_SWEEP_LAST.get(key)
        if last is not None and now - last < interval_seconds:
            return
        _DATASET_TMP_SWEEP_LAST[key] = now
    _sweep_orphaned_tmp_parts(path, recursive=True)


def write_dataset(
    df: pl.DataFrame,
    data_root: str | Path,
    dataset: str,
    *,
    partition_by: tuple[str, ...] = ("date", "symbol"),
    append: bool = True,
) -> Path:
    root = ensure_data_root(data_root)
    with exclusive_file_lock(dataset_lock_path(root, dataset), stale_seconds=21_600, poll_seconds=0.01):
        return _write_dataset_unlocked(df, root, dataset, partition_by=partition_by, append=append)


def _write_dataset_unlocked(
    df: pl.DataFrame,
    root: Path,
    dataset: str,
    *,
    partition_by: tuple[str, ...],
    append: bool,
) -> Path:
    path = dataset_path(root, dataset)
    # storage-concurrency-4: opportunistically sweep orphaned `.*.tmp` part files
    # left by a prior crash before this write. Safe here because we hold the
    # dataset lock (no concurrent writer's in-flight temp can be clobbered).
    _sweep_orphaned_tmp_parts(path, recursive=False)
    _sweep_orphaned_tmp_parts_if_due(path)
    if df.is_empty():
        path.mkdir(parents=True, exist_ok=True)
        return path

    if "ts_ms" in df.columns:
        df = with_date_column(df)
    path.mkdir(parents=True, exist_ok=True)

    # Month-bucket the demo/paper ledgers regardless of the partition_by the caller
    # passed (every ledger writer passes partition_by=()): force the immutable-keyed
    # _ledger_month partition so the hot-path write touches only the current month,
    # not the whole-history monolith (reconcile-ledger-5). See LEDGER_BUCKET_SOURCE.
    if dataset in LEDGER_BUCKET_SOURCE:
        df = _with_ledger_month(df, dataset)
        if _LEDGER_MONTH_COL in df.columns:
            partition_by = (_LEDGER_MONTH_COL,)

    partition_cols = [col for col in partition_by if col in df.columns]
    if not partition_cols:
        _write_part(df, path / "part.parquet", dataset=dataset, append=append)
        return path

    for key, part in df.partition_by(partition_cols, as_dict=True, maintain_order=True).items():
        key_tuple = key if isinstance(key, tuple) else (key,)
        part_path = path
        for col, value in zip(partition_cols, key_tuple):
            part_path = part_path / f"{col}={value}"
        part_path.mkdir(parents=True, exist_ok=True)
        _write_part(part, part_path / "part.parquet", dataset=dataset, append=append)
    return path


def read_dataset(data_root: str | Path, dataset: str) -> pl.DataFrame:
    return read_dataset_columns(data_root, dataset)


def _partition_date_ge(file: Path, since: str) -> bool:
    """True if a `date=YYYY-MM-DD`-partitioned file is on/after ``since`` (or has no
    date partition, so it is never pruned). Lets a reader skip old partitions by
    path alone — no parquet opened for pruned dates."""
    for part in file.parts:
        if part.startswith("date="):
            return part[len("date="):] >= since
    return True


def read_dataset_columns(
    data_root: str | Path,
    dataset: str,
    *,
    columns: list[str] | None = None,
    since_date: str | None = None,
) -> pl.DataFrame:
    """Eagerly read a dataset, optionally projecting only ``columns``.

    ``columns=None`` reproduces ``read_dataset``'s full-frame contract exactly.
    Passing an explicit list pushes the projection into ``scan_parquet`` so
    polars only decodes the requested columns from each parquet file — a large
    saving for wide datasets (e.g. klines_1h) on hot read paths. Any requested
    column absent from a partition is tolerated; the projection is intersected
    with the on-disk schema before collecting.

    ``since_date`` (YYYY-MM-DD) prunes `date=`-partitioned files to that date
    forward BEFORE any parquet is opened — a forward-window backtest then never
    pays to read the multi-year tail. Non-date-partitioned files are never pruned.

    A canonical request (e.g. ``funding``) transparently resolves to the
    venue-specific variant actually present on the root (e.g.
    ``binance_usdm_funding``); see :func:`resolve_dataset_name`.
    """
    dataset = resolve_dataset_name(data_root, dataset)
    path = dataset_path(data_root, dataset)
    if not path.exists():
        return pl.DataFrame()
    # Take the same per-dataset lock that writers hold. write_dataset performs
    # read-modify-write under this lock, and writers replace files atomically
    # via temp-file rename; without a reader-side lock, a reader's
    # scan_parquet -> collect can straddle a rename and observe a torn file
    # ("Invalid thrift: end of file"). Acquiring the lock here serialises with
    # writers cheaply (<10ms typical) and guarantees readers see a consistent
    # snapshot of the dataset. The collect() below MUST stay inside the lock so
    # the actual file reads complete before a writer can rename underneath us.
    with exclusive_file_lock(dataset_lock_path(data_root, dataset), stale_seconds=21_600, poll_seconds=0.01):
        # When since_date is set and the dataset is top-level `date=`-partitioned,
        # prune at the DIRECTORY level before globbing: a full `**/*.parquet` walk of
        # a (date,symbol)-partitioned root is ~500k files / tens of seconds, almost
        # all of it discarded. Globbing only the kept date dirs avoids that walk.
        if since_date:
            top_date_dirs = [d for d in path.glob("date=*") if d.is_dir()]
            if top_date_dirs:
                kept = [d for d in top_date_dirs if d.name[len("date="):] >= since_date]
                files = sorted(f for d in kept for f in d.glob("**/*.parquet"))
            else:
                files = [f for f in sorted(path.glob("**/*.parquet")) if _partition_date_ge(f, since_date)]
        else:
            files = sorted(path.glob("**/*.parquet"))
        return _collect_ledger_files(files, dataset=dataset, columns=columns)


def _collect_ledger_files(
    files: list[Path],
    *,
    dataset: str,
    columns: list[str] | None,
) -> pl.DataFrame:
    """Union a set of parquet part files into one frame, transparently dropping the
    internal _ledger_month partition column so the returned frame is schema-identical
    to the legacy monolithic layout. For a bucketed ledger this unions the legacy
    part.parquet (if any) with the new _ledger_month=* buckets; a cross-bucket
    unique() (scoped to bucketed ledgers ONLY — non-ledger datasets keep the legacy
    no-read-dedup behavior to avoid a full-frame unique on every big-dataset read)
    guarantees a key present in BOTH the legacy file and a migrated bucket (the
    migration window) is never double-counted on read."""
    if not files:
        return pl.DataFrame()
    file_paths = [str(file) for file in files]
    try:
        lf = pl.scan_parquet(file_paths)
        names = lf.collect_schema().names()
        if _LEDGER_MONTH_COL in names:
            lf = lf.drop(_LEDGER_MONTH_COL)
            names = [n for n in names if n != _LEDGER_MONTH_COL]
        if columns is not None:
            lf = lf.select([col for col in columns if col in names])
        out = lf.collect()
    except (
        pl.exceptions.SchemaError,
        pl.exceptions.ColumnNotFoundError,
        pl.exceptions.SchemaFieldNotFoundError,
        pl.exceptions.StructFieldNotFoundError,
        pl.exceptions.ComputeError,
        pl.exceptions.ShapeError,
    ):
        # Month-bucketing spreads schema-drifting ledger rows (different writers /
        # eras add columns) across bucket files, so a unified scan_parquet can hit
        # SchemaError, ColumnNotFoundError, Schema/StructFieldNotFoundError, or a
        # ComputeError/ShapeError when the merged scan can't reconcile column shapes.
        # The legacy monolith never did (one diagonal_relaxed-merged file). Fall back
        # to per-file read + diagonal concat, which tolerates the drift (ledgers are
        # small, so the no-pushdown cost is nil). A genuinely torn/unreadable file
        # still raises out of the per-file read below -- fail-loud, not masked.
        frames = [pl.read_parquet(file) for file in file_paths]
        out = pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
        if _LEDGER_MONTH_COL in out.columns:
            out = out.drop(_LEDGER_MONTH_COL)
        if columns is not None and not out.is_empty():
            out = out.select([col for col in columns if col in out.columns])
    # Cross-bucket dedup, scoped to the month-bucketed ledgers: per-part-file dedup
    # in _write_part does not span the legacy monolith + a migrated bucket during the
    # migration window. Coalesce on the dataset key, preferring the freshest
    # updated_at_ms (mirrors _write_part recency), so a key in both is never doubled.
    if dataset in LEDGER_BUCKET_SOURCE and not out.is_empty():
        keys = [c for c in DATASET_KEYS.get(dataset, ()) if c in out.columns]
        if keys:
            if "updated_at_ms" in out.columns:
                # maintain_order=True mirrors _write_part: an UNSTABLE sort can
                # reorder rows that share a null/equal updated_at_ms (preflight ->
                # final continuous order rows), letting a stale preflight win the
                # cross-bucket dedup and resurrecting the double-book class.
                out = out.sort("updated_at_ms", nulls_last=False, maintain_order=True)
            out = out.unique(subset=keys, keep="last")
    return out


def read_ledger_window(
    data_root: str | Path,
    dataset: str,
    *,
    months_back: int = 3,
) -> pl.DataFrame:
    """Windowed read of a month-bucketed ledger: only the most-recent ``months_back``
    month buckets plus the legacy monolith / _ledger_month=0 tail. quality-dup-5:
    ws_risk's per-reconcile read no longer scales with the daemon's whole-history
    ledger -- it only needs OPEN trades and recently-touched orders, and an open trade
    older than the window is still served by the always-included legacy tail until the
    migration drains it. Falls back to the full read for a non-bucketed dataset."""
    path = dataset_path(data_root, dataset)
    if not path.exists():
        return pl.DataFrame()
    with exclusive_file_lock(dataset_lock_path(data_root, dataset), stale_seconds=21_600, poll_seconds=0.01):
        bucket_dirs = _recent_ledger_month_dirs(path, months_back)
        if not bucket_dirs:
            files = sorted(path.glob("**/*.parquet"))
        else:
            legacy = sorted(path.glob("part.parquet"))
            files = legacy + sorted(f for d in bucket_dirs for f in d.glob("**/*.parquet"))
        return _collect_ledger_files(files, dataset=dataset, columns=None)


def _write_part(df: pl.DataFrame, path: Path, *, dataset: str, append: bool) -> None:
    # Invariant: only ever called from inside write_dataset, which holds
    # `exclusive_file_lock(dataset_lock_path(...))`. The pid + nanosecond temp
    # filename therefore can't collide with a concurrent writer — there ISN'T
    # one. If this is ever called from outside that lock, switch to a uuid4
    # temp name and re-derive the dedup story per dataset.
    _sweep_orphaned_tmp_parts(path.parent, recursive=False)
    output = df
    if append and path.exists():
        existing = pl.read_parquet(path)
        output = pl.concat([existing, output], how="diagonal_relaxed")
    keys = [col for col in DATASET_KEYS.get(dataset, ()) if col in output.columns]
    if keys:
        # Dedup by the dataset's natural keys. When rows carry updated_at_ms
        # (trades/orders), keep the freshest VERSION rather than whichever row
        # happened to be written last: two processes (the demo cycle and the
        # ws_risk engine) both author these rows, so write order is not a
        # reliable proxy for recency. Sort ascending (nulls — legacy rows with
        # no updated_at_ms — first) so the max updated_at_ms lands last and
        # unique(keep="last") wins it; among ties / nulls the concat order
        # (existing then new) still lets a same-version new row win.
        # maintain_order=True is load-bearing: continuous order rows (preflight ->
        # final) often share a null/equal updated_at_ms, and an UNSTABLE sort may
        # reorder ties — letting a stale preflight win the dedup and resurrecting
        # the double-book class (audit 2026-06-12 round 3 hardening).
        if "updated_at_ms" in output.columns:
            output = output.sort("updated_at_ms", nulls_last=False, maintain_order=True)
        output = output.unique(subset=keys, keep="last")
    sort_cols = [col for col in ("symbol", "ts_ms") if col in output.columns]
    if sort_cols:
        output = output.sort(sort_cols)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        output.write_parquet(temp_path)
        # fsync before the rename (round 4): the rename is atomic against a
        # PROCESS crash, but a hard power loss inside the page-cache window can
        # surface a truncated part file — and because this is a read-modify-
        # rewrite, that file is the only copy of the bucket's whole history
        # (the demo-forward evidence record). Cost is negligible at ledger
        # write rates.
        # O_RDWR, not O_RDONLY: Windows fsync (_commit) requires a WRITABLE
        # descriptor — a read-only fd raises EBADF and broke every dataset
        # write on the dev box (208 test failures, 2026-06-12).
        fd = os.open(temp_path, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        temp_path.replace(path)
        # storage-concurrency-5: fsync the PARENT DIRECTORY after the rename.
        # The file-fsync above makes the temp file's CONTENTS durable, but on
        # POSIX the rename itself (the directory entry now pointing `path` at the
        # new inode) is only durable after fsync of the containing directory fd.
        # Without it a hard power loss after replace() can revert the name to the
        # OLD inode, losing the most recent ledger update on a read-modify-rewrite
        # single-copy part file. Directory fsync is a no-op / unsupported on
        # Windows (O_RDONLY on a dir raises), so failures here are swallowed:
        # contents durability (the file-fsync) is the important guarantee and is
        # already met; this only tightens rename durability where the OS allows.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        temp_path.unlink(missing_ok=True)
