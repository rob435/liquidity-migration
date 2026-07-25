#!/usr/bin/env python3
"""Run a repository script with a POSIX-only stdlib stub on Windows (research only).

``liquidity_migration.storage`` (and a few lock helpers) import ``fcntl`` at
module top, which does not exist on Windows.  Read-only renders and
derived-artifact rebuilds are executed on the Windows research box, so this
wrapper installs a no-op ``fcntl`` stub into ``sys.modules`` before running the
target script in-process.

Scope and safety:
- The stub exists only inside this research process.  Operational entry points
  (VPS systemd services, ops.sh) run on Linux with the real ``fcntl`` and never
  import this wrapper.
- No-op locking is acceptable only for single-process research runs; do not
  run two stubbed writers against the same dataset concurrently.
- Research renders make no crash-durability or POSIX-durability claim.

Usage: .venv\\Scripts\\python.exe scripts/run_with_stub.py <script.py> [args...]
"""

from __future__ import annotations

import os
import runpy
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def ensure_posix_stubs() -> None:
    if os.name != "nt":
        return
    try:
        import fcntl  # noqa: F401
    except ModuleNotFoundError:
        # Attributes are set dynamically on a synthesised module, so mypy cannot
        # know them; the ignores are the price of the stub, not a defect.
        stub = types.ModuleType("fcntl")
        stub.LOCK_SH = 1  # type: ignore[attr-defined]
        stub.LOCK_EX = 2  # type: ignore[attr-defined]
        stub.LOCK_NB = 4  # type: ignore[attr-defined]
        stub.LOCK_UN = 8  # type: ignore[attr-defined]

        def _noop(*_args: object, **_kwargs: object) -> int:
            return 0

        stub.flock = _noop  # type: ignore[attr-defined]
        stub.lockf = _noop  # type: ignore[attr-defined]
        stub.fcntl = _noop  # type: ignore[attr-defined]
        stub.ioctl = _noop  # type: ignore[attr-defined]
        stub.__doc__ = "research-only no-op fcntl stub (single-process Windows research runs)"
        sys.modules["fcntl"] = stub

    # storage.exclusive_file_lock needs O_DIRECTORY/O_NOFOLLOW flock semantics
    # that Windows lacks.  Patch the single lock boundary with a thread-level
    # no-op BEFORE any caller from-imports it.  Single-process research only.
    import contextlib
    import threading

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
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
    # drop the optional projection.
    from liquidity_migration import account_kernel as account_kernel_module

    def _research_atomic_replace(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        if tmp.write_bytes(data) != len(data):
            raise OSError("research atomic account write made no progress")
        os.replace(tmp, path)

    account_kernel_module._atomic_replace = _research_atomic_replace
    account_kernel_module._append_jsonl_projection = lambda *_args, **_kwargs: None

    # continuous_btc_risk fsyncs files/directories through O_RDONLY descriptors,
    # which Windows rejects (Errno 9). Atomic-but-not-crash-durable is the same
    # trade already accepted above for the account kernel.
    from liquidity_migration import continuous_btc_risk as continuous_btc_risk_module

    continuous_btc_risk_module._fsync_file = lambda path: None
    continuous_btc_risk_module._fsync_directory = lambda path: None

    # artifact_snapshot.rename_noreplace deliberately fails closed off
    # Linux/Darwin (renameat2/renamex_np). Windows os.rename is natively
    # no-replace (FileExistsError when the destination exists), so the
    # research substitute keeps the evidence-preservation semantics while
    # dropping only the POSIX atomicity claim this wrapper already disclaims.
    from liquidity_migration import account_route as account_route_module
    from liquidity_migration import artifact_snapshot as artifact_snapshot_module

    def _research_rename_noreplace(
        source: "Path | str", destination: "Path | str", *, label: str
    ) -> None:
        try:
            os.rename(source, destination)
        except FileExistsError:
            raise FileExistsError(f"{label} already exists: {destination}") from None

    artifact_snapshot_module.rename_noreplace = _research_rename_noreplace
    account_route_module.rename_noreplace = _research_rename_noreplace
    # Directory fsync via an O_RDONLY descriptor is POSIX-only (Windows denies
    # opening directories as files). Same non-durable research trade as above.
    account_route_module._fsync_directory = lambda path: None


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    script = Path(sys.argv[1]).resolve()
    if REPO not in script.parents:
        raise SystemExit(f"refusing to run a script outside the repository: {script}")
    if not script.is_file():
        raise SystemExit(f"script not found: {script}")
    ensure_posix_stubs()
    sys.argv = [str(script), *sys.argv[2:]]
    runpy.run_path(str(script), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
