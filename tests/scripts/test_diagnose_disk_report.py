"""`mode=diagnose` must name the directories holding the filesystem.

A `capture-disk` page says the recorders stopped writing above their free-space
floor. It never says which writer took the space, and the single `df` line the
read-only diagnostic used to print cannot say either, so an on-call session
with no host access could not tell a tape at its cap from a foreign leak.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE = ROOT / "scripts/vps/deploy_remote.sh"


def _function(name: str) -> str:
    source = REMOTE.read_text(encoding="utf-8")
    body = source[source.index(f"{name}() {{") :]
    return body[: body.index("\n}\n") + 3]


def _report(roots: list[Path], *, lines: int = 20) -> list[str]:
    script = f"""
set -euo pipefail
DISK_REPORT_ROOTS={" ".join(str(root) for root in roots)!r}
DISK_REPORT_LINES={lines}
{_function("report_disk_usage")}
report_disk_usage
"""
    result = subprocess.run(
        ["bash"], input=script, text=True, capture_output=True, check=True
    )
    return result.stdout.splitlines()


def _tape(root: Path, name: str, megabytes: int) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "segment.zst").write_bytes(b"\0" * megabytes * 1024 * 1024)
    return directory


def test_the_report_names_each_directory_and_its_bytes_largest_first(tmp_path: Path) -> None:
    state = tmp_path / "liquidity-migration"
    state.mkdir()
    _tape(state, "forward-market", 6)
    _tape(state, "forward-market-binance", 3)
    _tape(state, "receipts", 1)

    lines = _report([state])

    assert all(line.startswith("disk ") for line in lines), lines
    named = [(int(line.split()[1]), line.split()[2]) for line in lines]
    assert [size for size, _ in named] == sorted((size for size, _ in named), reverse=True)
    paths = [path for _, path in named]
    assert str(state / "forward-market") in paths
    assert str(state / "forward-market-binance") in paths
    assert str(state / "receipts") in paths
    sizes = dict((path, size) for size, path in named)
    assert sizes[str(state / "forward-market")] > sizes[str(state / "forward-market-binance")]
    assert sizes[str(state / "forward-market-binance")] > sizes[str(state / "receipts")]


def test_the_report_stays_one_level_deep_and_prints_no_file_name(tmp_path: Path) -> None:
    state = tmp_path / "liquidity-migration"
    state.mkdir()
    hour = state / "forward-market" / "2026-09-07" / "22"
    hour.mkdir(parents=True)
    (hour / "bybit-linear.zst").write_bytes(b"\0" * 1024 * 1024)

    lines = _report([state])

    assert lines, lines
    assert not any("2026-09-07" in line for line in lines), lines
    assert not any(".zst" in line for line in lines), lines
    assert any(line.endswith(str(state / "forward-market")) for line in lines), lines


def test_a_root_that_does_not_exist_is_skipped_rather_than_failing_the_read(
    tmp_path: Path,
) -> None:
    state = tmp_path / "liquidity-migration"
    state.mkdir()
    _tape(state, "forward-market", 1)

    lines = _report([tmp_path / "absent", state, tmp_path / "also-absent"])

    assert any(line.endswith(str(state / "forward-market")) for line in lines), lines


def test_nested_roots_name_each_directory_once(tmp_path: Path) -> None:
    parent = tmp_path / "var-lib"
    state = parent / "liquidity-migration"
    state.mkdir(parents=True)
    _tape(state, "forward-market", 2)

    lines = _report([parent, state])

    paths = [line.split()[2] for line in lines]
    assert len(paths) == len(set(paths)), lines
    assert str(state) in paths
    assert str(state / "forward-market") in paths


def test_the_report_is_bounded_so_one_page_cannot_flood_the_run(tmp_path: Path) -> None:
    state = tmp_path / "liquidity-migration"
    state.mkdir()
    for index in range(12):
        _tape(state, f"tape-{index:02d}", 1)

    assert len(_report([state], lines=5)) == 5


def test_verify_mode_takes_the_reading() -> None:
    assert "report_disk_usage" in _function("verify_mode")
