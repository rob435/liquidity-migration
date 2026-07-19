#!/usr/bin/env python3
"""Run a repository script with a POSIX-only stdlib stub on Windows (research only).

``liquidity_migration.storage`` (and a few lock helpers) import ``fcntl`` at
module top, which does not exist on Windows.  The V3 research runbook executes
read-only renders and derived-artifact rebuilds on the Windows big PC, so this
wrapper installs a no-op ``fcntl`` stub into ``sys.modules`` before running the
target script in-process.

Scope and safety:
- The stub exists only inside this research process.  Operational entry points
  (VPS systemd services, ops.sh) run on Linux with the real ``fcntl`` and never
  import this wrapper.
- No-op locking is acceptable only for single-process research runs; do not
  run two stubbed writers against the same dataset concurrently.

Usage: .venv\\Scripts\\python.exe scripts/research_v3/run_with_stub.py <script.py> [args...]
"""

from __future__ import annotations

import os
import runpy
import sys
import types
from pathlib import Path


def ensure_posix_stubs() -> None:
    if os.name != "nt":
        return
    try:
        import fcntl  # noqa: F401
    except ModuleNotFoundError:
        stub = types.ModuleType("fcntl")
        stub.LOCK_SH = 1
        stub.LOCK_EX = 2
        stub.LOCK_NB = 4
        stub.LOCK_UN = 8

        def _noop(*_args: object, **_kwargs: object) -> int:
            return 0

        stub.flock = _noop
        stub.lockf = _noop
        stub.fcntl = _noop
        stub.ioctl = _noop
        stub.__doc__ = "research-only no-op fcntl stub (single-process Windows research runs)"
        sys.modules["fcntl"] = stub

    # storage.exclusive_file_lock needs O_DIRECTORY/O_NOFOLLOW flock semantics
    # that Windows lacks.  Patch the single lock boundary with a thread-level
    # no-op BEFORE any caller from-imports it.  Single-process research only;
    # mirrors the portable-account-io monkeypatch precedent in
    # scripts/analyze_strategy_overhaul_v2.py.
    import contextlib
    import threading

    repo = Path(__file__).resolve().parents[2]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from liquidity_migration import storage as storage_module

    process_lock = threading.RLock()

    @contextlib.contextmanager
    def _research_process_lock(*_args: object, **_kwargs: object):
        with process_lock:
            yield

    storage_module.exclusive_file_lock = _research_process_lock

    # account_kernel durability opens directory fds for fsync (POSIX-only) and
    # its JSONL projection append uses os.fchmod (POSIX-only).  Replace the
    # write boundary with atomic-but-not-crash-durable Windows equivalents and
    # drop the optional projection, exactly as the V2 phase-3 portable account
    # IO did.  Research renders make no crash/POSIX durability claim.
    from liquidity_migration import account_kernel as account_kernel_module

    def _research_atomic_replace(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        if tmp.write_bytes(data) != len(data):
            raise OSError("research atomic account write made no progress")
        os.replace(tmp, path)

    account_kernel_module._atomic_replace = _research_atomic_replace
    account_kernel_module._append_jsonl_projection = lambda *_args, **_kwargs: None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    script = Path(sys.argv[1]).resolve()
    repo = Path(__file__).resolve().parents[2]
    if repo not in script.parents:
        raise SystemExit(f"refusing to run a script outside the repository: {script}")
    if not script.is_file():
        raise SystemExit(f"script not found: {script}")
    ensure_posix_stubs()
    sys.argv = [str(script), *sys.argv[2:]]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
