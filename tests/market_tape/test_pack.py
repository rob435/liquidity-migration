"""The hourly packer: what it packs, what it leaves, and what it proves landed."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_tape import pack

ROOT = Path(pack.__file__).resolve().parents[1]
REMOTE = "gdrive:LiquidityMigration/market-tape/bybit-linear"
REMOTE_BASE = "gdrive:LiquidityMigration/market-tape"

FAKE_RCLONE = '''#!/usr/bin/env python3
"""A stand-in rclone: copyto stores bytes under FAKE_REMOTE_DIR, lsjson hashes them back."""
import hashlib, json, os, shutil, sys
args = sys.argv[1:]
log = open(os.environ["FAKE_RCLONE_LOG"], "a")
log.write(" ".join(args) + "\\n")
remote_root = os.environ["FAKE_REMOTE_DIR"]
def local(remote):
    return os.path.join(remote_root, remote.split(":", 1)[1])
command = args[0]
if command == "copyto":
    if os.environ.get("FAKE_RCLONE_CORRUPT") == "1":
        os.makedirs(os.path.dirname(local(args[2])), exist_ok=True)
        with open(local(args[2]), "wb") as handle:
            handle.write(b"not what was sent")
    else:
        os.makedirs(os.path.dirname(local(args[2])), exist_ok=True)
        shutil.copyfile(args[1], local(args[2]))
elif command == "lsjson":
    directory = local(args[1])
    rows = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                digest = hashlib.md5(open(path, "rb").read()).hexdigest()
                rows.append({"Name": name, "Size": os.path.getsize(path), "Hashes": {"md5": digest}})
    print(json.dumps(rows))
elif command == "about":
    print(json.dumps({"total": 5 * 1024**4, "used": 15 * 1024**3, "free": 4 * 1024**4}))
'''


def _fake_rclone(tmp_path: Path) -> Path:
    executable = tmp_path / "rclone"
    executable.write_text(FAKE_RCLONE, encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _segment(root: Path, day: str, hour: str, symbol: str, index: int, payload: bytes = b"zst-bytes") -> Path:
    path = root / day / hour / symbol / f"segment-{index:06d}.jsonl.zst"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _epoch(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def _run(tmp_path: Path, *tape_arguments: str, now: str, corrupt: bool = False) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "FAKE_RCLONE_LOG": str(tmp_path / "rclone.log"),
        "FAKE_REMOTE_DIR": str(tmp_path / "remote"),
        "FAKE_RCLONE_CORRUPT": "1" if corrupt else "0",
    }
    seed = tmp_path / "rclone.conf"
    seed.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "market_tape",
            "pack",
            *tape_arguments,
            "--state-dir",
            str(tmp_path / "state"),
            "--stamp-file",
            str(tmp_path / "receipts" / "market-tape-upload.last-success"),
            "--rclone",
            str(_fake_rclone(tmp_path)),
            "--config",
            str(tmp_path / "state" / "rclone.conf"),
            "--config-seed",
            str(seed),
            "--now",
            str(_epoch(now)),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _single_tape(tmp_path: Path) -> tuple[str, ...]:
    return ("--root", str(tmp_path / "tape"), "--remote", REMOTE)


def _ledger(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "state" / "uploaded-tapes.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_only_hours_that_ended_and_closed_are_packed(tmp_path: Path) -> None:
    root = tmp_path / "tape"
    _segment(root, "2026-09-02", "10", "BTCUSDT", 0)
    _segment(root, "2026-09-02", "10", "ETHUSDT", 0)
    (root / "2026-09-02" / "10" / "_meta").mkdir()
    (root / "2026-09-02" / "10" / "_meta" / "instruments-x.json.zst").write_bytes(b"meta")
    # Hour 11 has a segment still open: not finished.
    _segment(root, "2026-09-02", "11", "BTCUSDT", 0)
    (root / "2026-09-02" / "11" / "BTCUSDT" / "segment-000001.jsonl.partial").write_bytes(b"open")
    # Hour 12 is the current hour.
    _segment(root, "2026-09-02", "12", "BTCUSDT", 0)
    # A day in the older daily layout, already complete.
    legacy = root / "2026-08-30" / "SOLUSDT" / "segment-000000.jsonl.zst"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    # Today's daily-layout portion stays until the day ends.
    today_legacy = root / "2026-09-02" / "SOLUSDT" / "segment-000000.jsonl.zst"
    today_legacy.parent.mkdir(parents=True)
    today_legacy.write_bytes(b"legacy-today")

    now = _epoch("2026-09-02T12:20:00")
    names = [c.name for c in pack.finished_candidates(root, now=now, grace_seconds=300)]

    assert names == ["2026-08-30.legacy", "2026-09-02T10Z"]
    # Hour 11 ended at 12:00 but is still open; with the partial gone it packs.
    (root / "2026-09-02" / "11" / "BTCUSDT" / "segment-000001.jsonl.partial").unlink()
    names = [c.name for c in pack.finished_candidates(root, now=now, grace_seconds=300)]
    assert names == ["2026-08-30.legacy", "2026-09-02T10Z", "2026-09-02T11Z"]
    # Inside the grace window the hour is not yet packed.
    names = [c.name for c in pack.finished_candidates(root, now=_epoch("2026-09-02T12:03:00"), grace_seconds=300)]
    assert "2026-09-02T11Z" not in names


def test_an_hour_ships_as_one_archive_with_a_manifest_and_is_ledgered(tmp_path: Path) -> None:
    root = tmp_path / "tape"
    _segment(root, "2026-09-02", "10", "BTCUSDT", 0, b"btc-0")
    _segment(root, "2026-09-02", "10", "BTCUSDT", 1, b"btc-1")
    _segment(root, "2026-09-02", "10", "ETHUSDT", 0, b"eth-0")
    (root / "manifest.jsonl").write_text(
        json.dumps(
            {
                "kind": "segment_compressed",
                "path": "2026-09-02/10/BTCUSDT/segment-000000.jsonl.zst",
                "symbol": "BTCUSDT",
                "records": 42,
                "first_receive_ns": 1,
                "last_receive_ns": 2,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run(tmp_path, *_single_tape(tmp_path), now="2026-09-02T11:10:00")

    assert result.returncode == 0, result.stderr
    remote_tar = tmp_path / "remote" / "LiquidityMigration/market-tape/bybit-linear/2026/09/02/2026-09-02T10Z.tar"
    assert remote_tar.exists()
    with tarfile.open(remote_tar) as archive:
        names = archive.getnames()
        assert names[0] == "MANIFEST.json"
        assert set(names[1:]) == {
            "BTCUSDT/segment-000000.jsonl.zst",
            "BTCUSDT/segment-000001.jsonl.zst",
            "ETHUSDT/segment-000000.jsonl.zst",
        }
        manifest = json.load(archive.extractfile("MANIFEST.json"))
        assert archive.extractfile("BTCUSDT/segment-000001.jsonl.zst").read() == b"btc-1"
    assert manifest["kind"] == "market_tape_hour"
    assert manifest["day"] == "2026-09-02" and manifest["hour"] == "10"
    # The single-tape form takes its tape name from the last part of the remote.
    assert manifest["tape"] == "bybit-linear"
    assert manifest["symbols"] == ["BTCUSDT"]  # only the receipted file names its symbol
    first = next(row for row in manifest["files"] if row["path"] == "BTCUSDT/segment-000000.jsonl.zst")
    assert first["records"] == 42
    assert first["sha256"] == hashlib.sha256(b"btc-0").hexdigest()
    ledger = _ledger(tmp_path)
    assert [row["name"] for row in ledger] == ["2026-09-02T10Z"]
    assert ledger[0]["tape"] == "bybit-linear"
    assert ledger[0]["remote_path"] == f"{REMOTE}/2026/09/02/2026-09-02T10Z.tar"
    assert ledger[0]["md5"] == hashlib.md5(remote_tar.read_bytes()).hexdigest()
    stamp = (tmp_path / "receipts" / "market-tape-upload.last-success").read_text()
    assert "archives=bybit-linear/2026-09-02T10Z" in stamp
    assert "file_count=3" in stamp
    assert f"remote_free_bytes={4 * 1024**4}" in stamp
    assert f"destination={REMOTE}" in stamp
    # The staged tar is gone; the local segments stay for the recorder's retention.
    assert not list((tmp_path / "state" / "staging").glob("*.tar"))
    assert (root / "2026-09-02" / "10" / "BTCUSDT" / "segment-000000.jsonl.zst").exists()
    # A second run ships nothing new and does not re-upload.
    log_before = (tmp_path / "rclone.log").read_text()
    again = _run(tmp_path, *_single_tape(tmp_path), now="2026-09-02T11:20:00")
    assert again.returncode == 0, again.stderr
    assert (tmp_path / "rclone.log").read_text().count("copyto") == log_before.count("copyto")


def test_a_daily_layout_day_ships_whole_once_the_day_ended(tmp_path: Path) -> None:
    root = tmp_path / "tape"
    for symbol in ("BTCUSDT", "SOLUSDT"):
        path = root / "2026-08-30" / symbol / "segment-000000.jsonl.zst"
        path.parent.mkdir(parents=True)
        path.write_bytes(symbol.encode())

    result = _run(tmp_path, *_single_tape(tmp_path), now="2026-08-31T00:15:00")

    assert result.returncode == 0, result.stderr
    remote_tar = tmp_path / "remote" / "LiquidityMigration/market-tape/bybit-linear/2026/08/30/2026-08-30.legacy.tar"
    with tarfile.open(remote_tar) as archive:
        assert set(archive.getnames()) == {
            "MANIFEST.json",
            "BTCUSDT/segment-000000.jsonl.zst",
            "SOLUSDT/segment-000000.jsonl.zst",
        }
        manifest = json.load(archive.extractfile("MANIFEST.json"))
    assert manifest["kind"] == "market_tape_legacy_day"
    assert manifest["hour"] is None
    assert manifest["tape"] == "bybit-linear"


def test_a_corrupted_upload_is_not_ledgered_and_leaves_no_receipt(tmp_path: Path) -> None:
    _segment(tmp_path / "tape", "2026-09-02", "10", "BTCUSDT", 0)

    result = _run(tmp_path, *_single_tape(tmp_path), now="2026-09-02T11:10:00", corrupt=True)

    assert result.returncode != 0
    assert "not the uploaded" in result.stderr
    assert not (tmp_path / "state" / "uploaded-tapes.jsonl").exists()
    assert not (tmp_path / "receipts" / "market-tape-upload.last-success").exists()
    assert not list((tmp_path / "state" / "staging").glob("*.tar"))


def test_seeds_a_private_runtime_config_copy(tmp_path: Path) -> None:
    (tmp_path / "tape").mkdir()

    result = _run(tmp_path, *_single_tape(tmp_path), now="2026-09-02T11:10:00")

    assert result.returncode == 0, result.stderr
    runtime_config = tmp_path / "state" / "rclone.conf"
    assert runtime_config.read_text(encoding="utf-8") == "[gdrive]\ntype = drive\n"
    assert runtime_config.stat().st_mode & 0o777 == 0o600


def test_two_tapes_ship_under_their_own_folders_and_the_ledger_keys_by_remote_path(tmp_path: Path) -> None:
    bybit = tmp_path / "bybit"
    binance = tmp_path / "binance"
    binance.mkdir(parents=True)
    _segment(bybit, "2026-09-02", "10", "BTCUSDT", 0, b"bybit-btc")
    tapes = ("--tape", f"bybit-linear={bybit}", "--tape", f"binance-usdm={binance}", "--remote-base", REMOTE_BASE)

    first = _run(tmp_path, *tapes, now="2026-09-02T11:10:00")
    assert first.returncode == 0, first.stderr

    # The same hour on the second tape ships after the first tape's hour is ledgered.
    _segment(binance, "2026-09-02", "10", "BTCUSDT", 0, b"binance-btc")
    second = _run(tmp_path, *tapes, now="2026-09-02T11:20:00")
    assert second.returncode == 0, second.stderr

    remote = tmp_path / "remote" / "LiquidityMigration" / "market-tape"
    bybit_tar = remote / "bybit-linear" / "2026/09/02/2026-09-02T10Z.tar"
    binance_tar = remote / "binance-usdm" / "2026/09/02/2026-09-02T10Z.tar"
    assert bybit_tar.exists() and binance_tar.exists()
    with tarfile.open(binance_tar) as archive:
        manifest = json.load(archive.extractfile("MANIFEST.json"))
        assert archive.extractfile("BTCUSDT/segment-000000.jsonl.zst").read() == b"binance-btc"
    assert manifest["tape"] == "binance-usdm"
    ledger = _ledger(tmp_path)
    assert [row["name"] for row in ledger] == ["2026-09-02T10Z", "2026-09-02T10Z"]
    assert [row["tape"] for row in ledger] == ["bybit-linear", "binance-usdm"]
    assert [row["remote_path"] for row in ledger] == [
        f"{REMOTE_BASE}/bybit-linear/2026/09/02/2026-09-02T10Z.tar",
        f"{REMOTE_BASE}/binance-usdm/2026/09/02/2026-09-02T10Z.tar",
    ]
    assert (tmp_path / "rclone.log").read_text().count("copyto") == 2
    stamp = (tmp_path / "receipts" / "market-tape-upload.last-success").read_text()
    assert "archives=binance-usdm/2026-09-02T10Z" in stamp
    assert "tapes=bybit-linear,binance-usdm" in stamp
    assert f"destination={REMOTE_BASE}" in stamp

    # A third run has nothing left for either tape.
    third = _run(tmp_path, *tapes, now="2026-09-02T11:30:00")
    assert third.returncode == 0, third.stderr
    assert (tmp_path / "rclone.log").read_text().count("copyto") == 2
    assert "archives=none" in (tmp_path / "receipts" / "market-tape-upload.last-success").read_text()


def test_a_tape_whose_root_is_missing_is_noted_and_the_others_ship(tmp_path: Path) -> None:
    bybit = tmp_path / "bybit"
    _segment(bybit, "2026-09-02", "10", "BTCUSDT", 0)
    absent = tmp_path / "binance"

    result = _run(
        tmp_path,
        "--tape",
        f"bybit-linear={bybit}",
        "--tape",
        f"binance-usdm={absent}",
        "--remote-base",
        REMOTE_BASE,
        now="2026-09-02T11:10:00",
    )

    assert result.returncode == 0, result.stderr
    assert "binance-usdm has no root yet" in result.stderr
    assert (tmp_path / "remote" / "LiquidityMigration/market-tape/bybit-linear/2026/09/02/2026-09-02T10Z.tar").exists()
    assert [row["tape"] for row in _ledger(tmp_path)] == ["bybit-linear"]
    assert "tapes=bybit-linear" in (tmp_path / "receipts" / "market-tape-upload.last-success").read_text()


def test_no_tape_root_at_all_ships_nothing(tmp_path: Path) -> None:
    result = _run(tmp_path, "--tape", f"bybit-linear={tmp_path / 'nowhere'}", "--remote-base", REMOTE_BASE, now="2026-09-02T11:10:00")

    assert result.returncode == 2
    assert "no tape root exists" in result.stderr
    assert not (tmp_path / "receipts" / "market-tape-upload.last-success").exists()


def test_a_tape_needs_a_name_a_root_and_a_remote(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def namespace(**values: object) -> argparse.Namespace:
        return argparse.Namespace(**{"tape": [], "remote_base": None, "root": None, "remote": None, **values})

    def refused(text: str, **values: object) -> None:
        # A malformed invocation exits 2 and says why on stderr.
        with pytest.raises(SystemExit) as excinfo:
            pack.parse_tapes(namespace(**values))
        assert excinfo.value.code == 2
        assert text in capsys.readouterr().err

    refused("at least one tape")
    refused("remote-base", tape=["bybit=/var/tape"])
    refused("NAME=ROOT", tape=["/var/tape"], remote_base=REMOTE_BASE)
    refused("NAME=ROOT", tape=["a/b=/var/tape"], remote_base=REMOTE_BASE)
    refused("go together", root=Path("/var/tape"))

    both = pack.parse_tapes(namespace(tape=["bybit=/var/tape"], remote_base=REMOTE_BASE + "/", root=Path("/var/other"), remote=REMOTE))
    assert [(tape.name, tape.root, tape.remote) for tape in both] == [
        ("bybit", Path("/var/tape").resolve(), f"{REMOTE_BASE}/bybit"),
        ("bybit-linear", Path("/var/other").resolve(), REMOTE),
    ]


def test_a_ledger_row_without_a_remote_path_still_counts(tmp_path: Path) -> None:
    path = tmp_path / "uploaded-tapes.jsonl"
    path.write_text(
        json.dumps({"name": "2026-09-02T10Z", "bytes": 1})
        + "\n\n"
        + json.dumps({"name": "2026-09-02T11Z", "remote_path": f"{REMOTE}/2026/09/02/2026-09-02T11Z.tar"})
        + "\n",
        encoding="utf-8",
    )

    ledger = pack.load_ledger(path)

    assert sorted(ledger) == ["2026-09-02T10Z", f"{REMOTE}/2026/09/02/2026-09-02T11Z.tar"]
    assert pack.load_ledger(tmp_path / "absent.jsonl") == {}


def test_the_stamp_sums_the_last_thirty_days_of_uploads() -> None:
    ledger = {
        "r/a": {"uploaded_at": "2026-09-01T10:00:00Z", "bytes": 100},
        "r/b": {"uploaded_at": "2026-08-01T10:00:00Z", "bytes": 1000},
        "r/c": {"uploaded_at": "2026-09-02T00:00:00Z", "bytes": 5},
        "r/d": {"uploaded_at": "garbage", "bytes": 7},
    }
    since = datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp()
    assert pack.bytes_uploaded_since(ledger, since) == 105
    assert pack.bytes_uploaded_since({}, since) == 0


def test_a_failed_archive_build_leaves_no_partial_archive_in_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial `.tar.tmp` is disk no retention pass can see: staging is outside
    both tape roots, on the filesystem the recorders' free-space floor guards."""

    root = tmp_path / "tape"
    _segment(root, "2026-09-02", "10", "BTCUSDT", 0)
    staging = tmp_path / "state" / "staging"
    candidate = pack.Candidate("2026-09-02T10Z", "2026-09-02", "10", (root / "2026-09-02" / "10",))
    added = 0
    real_addfile = tarfile.TarFile.addfile

    def full_disk(self, tarinfo, fileobj=None):  # noqa: ANN001, ANN202 - test double
        nonlocal added
        added += 1
        if added > 1:  # the manifest lands, then the disk fills
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_addfile(self, tarinfo, fileobj)

    monkeypatch.setattr(tarfile.TarFile, "addfile", full_disk)

    with pytest.raises(OSError):
        pack.build_archive(candidate, root, staging, {})

    assert list(staging.iterdir()) == []


def test_a_run_reclaims_the_staging_a_killed_run_left_behind(tmp_path: Path) -> None:
    _segment(tmp_path / "tape", "2026-09-02", "10", "BTCUSDT", 0)
    staging = tmp_path / "state" / "staging"
    staging.mkdir(parents=True)
    orphans = (staging / "2026-09-02T09Z.tar", staging / ".2026-09-02T08Z.tar.tmp")
    for orphan in orphans:
        orphan.write_bytes(b"x" * 1024)
    keep = staging / "notes.txt"
    keep.write_bytes(b"not an archive")

    result = _run(tmp_path, *_single_tape(tmp_path), now="2026-09-02T11:20:00")

    assert result.returncode == 0, result.stderr
    assert "removed stale staging archive 2026-09-02T09Z.tar bytes=1024" in result.stdout
    assert "removed stale staging archive .2026-09-02T08Z.tar.tmp bytes=1024" in result.stdout
    for orphan in orphans:
        assert not orphan.exists()
    assert keep.exists()
    # The run still ships its own hour.
    assert [row["name"] for row in _ledger(tmp_path)] == ["2026-09-02T10Z"]
