from __future__ import annotations

import multiprocessing
from pathlib import Path

from liquidity_migration.account_owner_lease import AccountOwnerLease


def _try_lease(path: str, output: multiprocessing.Queue) -> None:
    try:
        with AccountOwnerLease(path):
            output.put("acquired")
    except RuntimeError as exc:
        output.put(str(exc))


def test_only_one_process_can_hold_account_owner_lease(tmp_path: Path) -> None:
    path = tmp_path / "owner.lock"
    output: multiprocessing.Queue = multiprocessing.Queue()
    with AccountOwnerLease(path):
        process = multiprocessing.Process(target=_try_lease, args=(str(path), output))
        process.start()
        process.join(timeout=5)
        assert process.exitcode == 0
        assert "already held" in output.get(timeout=1)

    with AccountOwnerLease(path):
        pass
