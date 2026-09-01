"""The hourly market-tape packer: what it packs, what it leaves, and what it proves landed."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "runtime" / "pack_market_tape.py"

spec = importlib.util.spec_from_file_location("pack_market_tape", SCRIPT)
assert spec is not None and spec.loader is not None
packer = importlib.util.module_from_spec(spec)
sys.modules["pack_market_tape"] = packer
spec.loader.exec_module(packer)

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
    # A legacy day layout from before hourly segments, already complete.
    legacy = root / "2026-08-30" / "SOLUSDT" / "segment-000000.jsonl.zst"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"legacy")
    # Today's legacy portion stays until the day ends.
    today_legacy = root / "2026-09-02" / "SOLUSDT" / "segment-000000.jsonl.zst"
    today_legacy.parent.mkdir(parents=True)
    today_legacy.write_bytes(b"legacy-today")

    now = _epoch("2026-09-02T12:20:00")
    names = [c.name for c in packer.finished_candidates(root, now=now, grace_seconds=300)]

    assert names == ["2026-08-30.legacy", "2026-09-02T10Z"]
    # Hour 11 ended at 12:00 but is still open; with the partial gone it packs.
    (root / "2026-09-02" / "11" / "BTCUSDT" / "segment-000001.jsonl.partial").unlink()
    names = [c.name for c in packer.finished_candidates(root, now=now, grace_seconds=300)]
    assert names == ["2026-08-30.legacy", "2026-09-02T10Z", "2026-09-02T11Z"]
    # Inside the grace window the hour is not yet packed.
    names = [c.name for c in packer.finished_candidates(root, now=_epoch("2026-09-02T12:03:00"), grace_seconds=300)]
    assert "2026-09-02T11Z" not in names


def _run(tmp_path: Path, *, now: str, corrupt: bool = False) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "FAKE_RCLONE_LOG": str(tmp_path / "rclone.log"),
        "FAKE_REMOTE_DIR": str(tmp_path / "remote"),
        "FAKE_RCLONE_CORRUPT": "1" if corrupt else "0",
    }
    config = tmp_path / "rclone.conf"
    config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--root",
            str(tmp_path / "tape"),
            "--remote",
            "gdrive:LiquidityMigration/market-tape/bybit-linear",
            "--state-dir",
            str(tmp_path / "state"),
            "--stamp-file",
            str(tmp_path / "receipts" / "market-tape-upload.last-success"),
            "--rclone",
            str(_fake_rclone(tmp_path)),
            "--config",
            str(tmp_path / "state" / "rclone.conf"),
            "--config-seed",
            str(config),
            "--now",
            str(_epoch(now)),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


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

    result = _run(tmp_path, now="2026-09-02T11:10:00")

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
    assert manifest["symbols"] == ["BTCUSDT"]  # only the receipted file names its symbol
    first = next(row for row in manifest["files"] if row["path"] == "BTCUSDT/segment-000000.jsonl.zst")
    assert first["records"] == 42
    assert first["sha256"] == hashlib.sha256(b"btc-0").hexdigest()
    ledger = [json.loads(line) for line in (tmp_path / "state" / "uploaded-tapes.jsonl").read_text().splitlines()]
    assert [row["name"] for row in ledger] == ["2026-09-02T10Z"]
    assert ledger[0]["md5"] == hashlib.md5(remote_tar.read_bytes()).hexdigest()
    stamp = (tmp_path / "receipts" / "market-tape-upload.last-success").read_text()
    assert "archives=2026-09-02T10Z" in stamp
    assert "file_count=3" in stamp
    assert f"remote_free_bytes={4 * 1024**4}" in stamp
    # The staged tar is gone; the local segments stay for the recorder's retention.
    assert not list((tmp_path / "state" / "staging").glob("*.tar"))
    assert (root / "2026-09-02" / "10" / "BTCUSDT" / "segment-000000.jsonl.zst").exists()
    # A second run ships nothing new and does not re-upload.
    log_before = (tmp_path / "rclone.log").read_text()
    again = _run(tmp_path, now="2026-09-02T11:20:00")
    assert again.returncode == 0, again.stderr
    assert (tmp_path / "rclone.log").read_text().count("copyto") == log_before.count("copyto")


def test_a_legacy_day_ships_whole_once_the_day_ended(tmp_path: Path) -> None:
    root = tmp_path / "tape"
    for symbol in ("BTCUSDT", "SOLUSDT"):
        path = root / "2026-08-30" / symbol / "segment-000000.jsonl.zst"
        path.parent.mkdir(parents=True)
        path.write_bytes(symbol.encode())

    result = _run(tmp_path, now="2026-08-31T00:15:00")

    assert result.returncode == 0, result.stderr
    remote_tar = tmp_path / "remote" / "LiquidityMigration/market-tape/bybit-linear/2026/08/30/2026-08-30.legacy.tar"
    with tarfile.open(remote_tar) as archive:
        assert set(archive.getnames()) == {
            "MANIFEST.json",
            "BTCUSDT/segment-000000.jsonl.zst",
            "SOLUSDT/segment-000000.jsonl.zst",
        }
        assert json.load(archive.extractfile("MANIFEST.json"))["kind"] == "market_tape_legacy_day"


def test_a_corrupted_upload_is_not_ledgered_and_leaves_no_receipt(tmp_path: Path) -> None:
    root = tmp_path / "tape"
    _segment(root, "2026-09-02", "10", "BTCUSDT", 0)

    result = _run(tmp_path, now="2026-09-02T11:10:00", corrupt=True)

    assert result.returncode != 0
    assert "not the uploaded" in result.stderr
    assert not (tmp_path / "state" / "uploaded-tapes.jsonl").exists()
    assert not (tmp_path / "receipts" / "market-tape-upload.last-success").exists()


def test_seeds_a_private_runtime_config_copy(tmp_path: Path) -> None:
    root = tmp_path / "tape"
    root.mkdir()
    result = _run(tmp_path, now="2026-09-02T11:10:00")
    assert result.returncode == 0, result.stderr
    runtime_config = tmp_path / "state" / "rclone.conf"
    assert runtime_config.read_text(encoding="utf-8") == "[gdrive]\ntype = drive\n"
    assert runtime_config.stat().st_mode & 0o777 == 0o600


def test_systemd_unit_runs_the_packer_against_the_recorder_root_and_receipts() -> None:
    unit = (ROOT / "deploy" / "systemd" / "liquidity-migration-market-tape-upload.service").read_text(encoding="utf-8")
    assert "ExecStart=/opt/liquidity-migration/.venv/bin/python scripts/runtime/pack_market_tape.py" in unit
    assert "--root /var/lib/liquidity-migration/forward-market" in unit
    assert "--remote gdrive:LiquidityMigration/market-tape/bybit-linear" in unit
    assert "--stamp-file /var/lib/liquidity-migration/receipts/market-tape-upload.last-success" in unit
    assert "Environment=RCLONE_CONFIG=/var/lib/liquidity-migration/market-tape-upload/rclone.conf" in unit
    assert "Environment=RCLONE_CONFIG_SEED=/etc/liquidity-migration/rclone.conf" in unit
    timer = (ROOT / "deploy" / "systemd" / "liquidity-migration-market-tape-upload.timer").read_text(encoding="utf-8")
    assert "Persistent=true" in timer
