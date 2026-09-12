"""The hourly storage reclaimer: budgets, verified-history deletion, and hygiene."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from liquidity_migration.policy.realms import realms as realm_table

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "reclaim_host_storage", ROOT / "scripts/runtime/reclaim_host_storage.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules["reclaim_host_storage"] = MODULE
SPEC.loader.exec_module(MODULE)

GIB = 1024**3
MIB = 1024 * 1024
NOW = 1_760_000_000.0
REMOTE = "gdrive:LiquidityMigration/engine-state"
COMMITS = {
    "deployed": "a" * 40,
    "previous": "b" * 40,
    "keep": "c" * 40,
    "override": "d" * 40,
    "young": "e" * 40,
    "stale": "f" * 40,
    "stale2": "0" * 40,
}

RCLONE_STUB = '''
import hashlib
import json
import os
import sys
from pathlib import Path

REMOTE_ROOT = Path(os.environ["STUB_REMOTE_ROOT"])
LOG = Path(os.environ["STUB_RCLONE_LOG"])
CORRUPT = os.environ.get("STUB_SEALED_CORRUPT", "")
VALUE_FLAGS = {"--config", "--retries", "--low-level-retries", "--transfers", "--checkers"}


def local(spec):
    if ":" in spec:
        return REMOTE_ROOT / spec.split(":", 1)[1].lstrip("/")
    return Path(spec)


args = sys.argv[1:]
with LOG.open("a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")
positional = []
index = 0
while index < len(args):
    item = args[index]
    if item in VALUE_FLAGS:
        index += 2
        continue
    if item.startswith("--"):
        index += 1
        continue
    positional.append(item)
    index += 1
command = positional[0]
rest = positional[1:]
if command == "lsjson":
    directory = local(rest[0])
    if not directory.is_dir():
        print("directory not found: " + rest[0], file=sys.stderr)
        raise SystemExit(3)
    rows = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        data = path.read_bytes()
        rows.append({
            "Path": path.name,
            "Name": path.name,
            "Size": len(data),
            "Hashes": {"md5": hashlib.md5(data).hexdigest()},
        })
    print(json.dumps(rows))
    raise SystemExit(0)
if command == "copyto":
    source = local(rest[0])
    destination = local(rest[1])
    if not source.is_file():
        print("object not found: " + rest[0], file=sys.stderr)
        raise SystemExit(3)
    payload = source.read_bytes()
    if CORRUPT and "/sealed/" in rest[1]:
        payload += b"corrupt"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    raise SystemExit(0)
print("unsupported: " + command, file=sys.stderr)
raise SystemExit(2)
'''

ENGINE_TOOLS_STUB = '''
import json
import os
import sys
from pathlib import Path

assert sys.argv[1] == "wal-retention", sys.argv
assert "--json" in sys.argv, sys.argv
family = sys.argv[sys.argv.index("--wal") + 1]
table = json.loads(Path(os.environ["STUB_RETENTION_JSON"]).read_text(encoding="utf-8"))
payload = table.get(family)
if payload is None:
    print("no retention for " + family, file=sys.stderr)
    raise SystemExit(1)
if payload == "malformed":
    print("{ not json", end="")
    raise SystemExit(0)
print(json.dumps(payload))
'''

APT_STUB = '''
import os
import sys
from pathlib import Path

Path(os.environ["STUB_APT_LOG"]).open("a", encoding="utf-8").write(" ".join(sys.argv[1:]) + "\\n")
'''


@dataclass(frozen=True)
class FakeStatvfs:
    f_blocks: int
    f_bfree: int
    f_bavail: int
    f_frsize: int = 4096
    f_favail: int = 1_000_000


def statvfs_for(capacity: int, free: int) -> FakeStatvfs:
    frsize = 4096
    return FakeStatvfs(f_blocks=capacity // frsize, f_bfree=free // frsize, f_bavail=free // frsize)


def as_gib(byte_count: int) -> str:
    return repr(byte_count / GIB)


def freeable(path: Path) -> int:
    return path.stat().st_blocks * 512


class Host:
    """A fake VPS under tmp_path: engine state dirs, a backup stage, a stand-in remote."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp = tmp_path
        self.host = tmp_path / "host"
        self.remote_root = tmp_path / "remote"
        self.state = self.host / "var/lib/liquidity-migration/storage-reclaim"
        self.stage = self.host / "var/lib/liquidity-migration/backup/stage"
        self.stamp = self.host / "var/lib/liquidity-migration/receipts/storage-reclaim.last-success"
        self.lock = self.host / "var/lib/liquidity-migration/backup/backup.lock"
        self.archive = self.host / "var/lib/liquidity-migration-wal-quarantine"
        self.release_dir = self.host / "opt/liquidity-migration-engine"
        self.systemd = self.host / "etc/systemd/system"
        self.binaries = tmp_path / "bin"
        self.binaries.mkdir(parents=True, exist_ok=True)
        self.capture = tmp_path / "capture"
        self.capture.mkdir(parents=True, exist_ok=True)
        self.rclone_log = tmp_path / "rclone.log"
        self.apt_log = tmp_path / "apt.log"
        self.retention_table = tmp_path / "retention.json"
        self.rclone_config = self.host / "var/lib/liquidity-migration/backup/rclone.conf"
        self.rclone_config.parent.mkdir(parents=True, exist_ok=True)
        self.rclone_config.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
        self.retention: dict[str, Any] = {}
        self.families: list[Path] = []
        self.rclone = self._stub("rclone", RCLONE_STUB)
        self.engine_tools = self._stub("engine-tools", ENGINE_TOOLS_STUB)
        self.apt = self._stub("apt-get", APT_STUB)
        self.retention_table.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("STUB_REMOTE_ROOT", str(self.remote_root))
        monkeypatch.setenv("STUB_RCLONE_LOG", str(self.rclone_log))
        monkeypatch.setenv("STUB_RETENTION_JSON", str(self.retention_table))
        monkeypatch.setenv("STUB_APT_LOG", str(self.apt_log))
        for name in os.environ:
            if name.startswith("RECLAIM_"):
                monkeypatch.delenv(name, raising=False)

    def _stub(self, name: str, body: str) -> Path:
        path = self.binaries / name
        path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
        path.chmod(0o755)
        return path

    # -- fixture construction -------------------------------------------

    def family(self, realm: str, *, current: int, floor: int) -> Path:
        directory = self.host / "var/lib" / f"liquidity-migration-engine-{realm}"
        directory.mkdir(parents=True, exist_ok=True)
        family = directory / "engine.wal"
        family.write_bytes(b"live tail")
        self.families.append(family)
        self.retention[str(family)] = {
            "family": str(family),
            "segments": [],
            "current_segment": current,
            "newest_trusted_segment": current - 1,
            "boot_fallback_segment": max(current - 1, 1),
            "callback_floor_segment": None,
            "retention_floor_segment": floor,
        }
        self.retention_table.write_text(json.dumps(self.retention), encoding="utf-8")
        return family

    def break_retention(self, family: Path, *, mode: str) -> None:
        if mode == "missing":
            self.retention.pop(str(family), None)
        else:
            self.retention[str(family)] = "malformed"
        self.retention_table.write_text(json.dumps(self.retention), encoding="utf-8")

    def segment(
        self,
        family: Path,
        index: int,
        *,
        age_hours: float = 100.0,
        size: int = 2 * MIB,
        stage: str = "link",
        remote: str = "same",
    ) -> Path:
        path = family.parent / f"engine.wal.{index:06d}"
        payload = bytes([index % 251]) * size
        path.write_bytes(payload)
        mtime = NOW - age_hours * 3600.0
        os.utime(path, (mtime, mtime))
        if remote != "absent":
            landed = self.remote_root / REMOTE.split(":", 1)[1] / "latest" / str(path.relative_to(path.anchor))
            landed.parent.mkdir(parents=True, exist_ok=True)
            if remote == "same":
                landed.write_bytes(payload)
            elif remote == "other_md5":
                landed.write_bytes(bytes([(index + 1) % 251]) * size)
            elif remote == "other_size":
                landed.write_bytes(payload + b"tail")
        staged = self.stage / str(path.relative_to(path.anchor))
        staged.parent.mkdir(parents=True, exist_ok=True)
        if stage == "link":
            os.link(path, staged)
        elif stage == "copy":
            staged.write_bytes(payload)
        return path

    def deploy_tree(self) -> None:
        releases = self.release_dir / "releases"
        staged = self.release_dir / "staged"
        releases.mkdir(parents=True, exist_ok=True)
        staged.mkdir(parents=True, exist_ok=True)
        old = NOW - 30 * 86_400.0
        for commit in COMMITS.values():
            directory = releases / commit
            (directory / "bin").mkdir(parents=True, exist_ok=True)
            (directory / "bin" / "engine").write_bytes(b"e" * 4096)
            tarball = staged / f"{commit}.tar.gz"
            tarball.write_bytes(b"t" * 4096)
            when = NOW - 60.0 if commit == COMMITS["young"] else old
            os.utime(directory, (when, when))
            os.utime(tarball, (when, when))
        (self.release_dir / "deployed-commit").write_text(COMMITS["deployed"] + "\n", encoding="utf-8")
        (self.release_dir / "previous-commit").write_text(COMMITS["previous"] + "\n", encoding="utf-8")
        (self.release_dir / "bin").mkdir(exist_ok=True)
        (self.release_dir / "bin" / "engine-tools").write_bytes(b"x")
        (self.release_dir / "checkpoint-configs").mkdir(exist_ok=True)
        (self.release_dir / "incumbent-demo").write_text("keep", encoding="utf-8")
        (self.release_dir / "engine.fingerprint").write_text("keep", encoding="utf-8")
        for name, age in (("qualified.old", 3 * 86_400.0), ("qualified.new", 60.0)):
            temporary = staged / f".{name}"
            temporary.mkdir(exist_ok=True)
            (temporary / "payload").write_bytes(b"p" * 512)
            os.utime(temporary, (NOW - age, NOW - age))
        override = self.systemd / "liquidity-migration-market-recorder.service.d"
        override.mkdir(parents=True, exist_ok=True)
        (override / "override.conf").write_text(
            f"[Service]\nExecStart=/opt/liquidity-migration-engine/releases/{COMMITS['override']}/bin/recorder\n",
            encoding="utf-8",
        )
        (self.systemd / "liquidity-migration-engine.service").write_text(
            f"[Service]\nEnvironment=COMMIT={COMMITS['deployed']}\n", encoding="utf-8"
        )

    def capture_config(self, name: str, text: str) -> Path:
        path = self.capture / f"{name}.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def argv(self, *extra: str) -> list[str]:
        families: list[str] = []
        for family in self.families:
            families += ["--wal-family", str(family)]
        return [
            "--filesystem",
            str(self.host),
            "--state-dir",
            str(self.state),
            "--stamp-file",
            str(self.stamp),
            "--remote",
            REMOTE,
            "--rclone-bin",
            str(self.rclone),
            "--rclone-config",
            str(self.rclone_config),
            "--engine-tools",
            str(self.engine_tools),
            "--backup-lock",
            str(self.lock),
            "--backup-stage",
            str(self.stage),
            "--release-dir",
            str(self.release_dir),
            "--systemd-dir",
            str(self.systemd),
            "--archive-root",
            str(self.archive),
            "--tape-floor-config",
            str(self.capture),
            "--no-apt-clean",
            *families,
            *extra,
        ]

    # -- running ---------------------------------------------------------

    def run(self, *extra: str, capacity: int = GIB, free: int = MIB) -> int:
        stats = statvfs_for(capacity, free)
        return MODULE.main(self.argv(*extra), statvfs=lambda _: stats, now=lambda: NOW)

    def status(self) -> dict[str, Any]:
        return json.loads((self.state / "status.json").read_text(encoding="utf-8"))

    def ledger(self) -> list[dict[str, Any]]:
        path = self.state / "ledger.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def rclone_calls(self) -> list[str]:
        if not self.rclone_log.exists():
            return []
        return self.rclone_log.read_text(encoding="utf-8").splitlines()


#: Pressure marks small enough to reach with megabyte fixtures.
PRESSURE = (
    "--reserve-fraction",
    "0",
    "--reserve-floor-gib",
    as_gib(6 * MIB),
    "--writer-headroom-gib",
    as_gib(2 * MIB),
    "--high-water-default-gib",
    as_gib(4 * MIB),
)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Host:
    return Host(tmp_path, monkeypatch)


def two_families(host: Host) -> tuple[Path, Path]:
    """Two families whose reclaimable segments interleave by age."""

    mainnet = host.family("mainnet", current=10, floor=8)
    mexc = host.family("mexc", current=8, floor=7)
    for index, age in ((2, 200.0), (3, 198.0), (4, 196.0), (5, 194.0), (6, 192.0)):
        host.segment(mainnet, index, age_hours=age)
    host.segment(mainnet, 7, age_hours=1.0)
    for index in (8, 9, 10):
        host.segment(mainnet, index, age_hours=0.5)
    for index, age in ((2, 199.0), (3, 197.0), (4, 195.0), (5, 193.0), (6, 191.0)):
        host.segment(mexc, index, age_hours=age)
    for index in (7, 8):
        host.segment(mexc, index, age_hours=0.5)
    return mainnet, mexc


def test_no_pressure_keeps_verified_history_and_still_runs_hygiene(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    mainnet, mexc = two_families(host)
    host.deploy_tree()
    monkeypatch.setenv("PATH", f"{host.binaries}{os.pathsep}{os.environ['PATH']}")
    argv = [item for item in host.argv(*PRESSURE, "--apt-clean") if item != "--no-apt-clean"]
    stats = statvfs_for(GIB, 64 * MIB)
    assert MODULE.main(argv, statvfs=lambda _: stats, now=lambda: NOW) == 0

    status = host.status()
    assert status["reclaimed_by_class"]["wal"] == 0
    assert status["retained_verified_wal_segments"] == 9
    expected = sum(
        freeable(family.parent / f"engine.wal.{index:06d}")
        for family, indexes in ((mainnet, range(2, 8)), (mexc, range(2, 6)))
        for index in indexes
        if not (family is mainnet and index == 7)
    )
    assert status["retained_verified_wal_bytes"] == expected
    for family, indexes in ((mainnet, range(1, 11)), (mexc, range(1, 9))):
        assert family.exists()
        for index in indexes:
            if index == 1:
                continue
            assert (family.parent / f"engine.wal.{index:06d}").exists()
    assert host.stamp.exists()
    stamp = dict(line.split("=", 1) for line in host.stamp.read_text(encoding="utf-8").splitlines())
    assert stamp["wal_segments_reclaimed"] == "0"
    assert stamp["reclaimed_bytes"] == str(status["reclaimed_bytes_this_run"])
    assert not (host.release_dir / "releases" / COMMITS["stale"]).exists()
    assert (host.release_dir / "releases" / COMMITS["deployed"]).is_dir()
    assert host.apt_log.read_text(encoding="utf-8").split() == ["clean"]
    assert (host.state / "md5-cache.json").exists()
    assert [row["measured_at_s"] for row in _samples(host)] == [NOW]
    # The watchdog and the operator read these as another user.
    assert stat.S_IMODE(host.state.stat().st_mode) == 0o755
    assert stat.S_IMODE(host.stamp.parent.stat().st_mode) == 0o755
    for name in ("status.json", "ledger.jsonl", "samples.jsonl"):
        assert stat.S_IMODE((host.state / name).stat().st_mode) == 0o644, name
    assert stat.S_IMODE(host.stamp.stat().st_mode) == 0o644


def _samples(host: Host) -> list[dict[str, Any]]:
    path = host.state / "samples.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_pressure_reclaims_the_oldest_verified_history_across_families(host: Host) -> None:
    mainnet, mexc = two_families(host)
    assert host.run(*PRESSURE) == 0

    status = host.status()
    reclaimed = [item for item in status["plan"] if item["class"] == "wal"]
    order = [Path(item["path"]).relative_to(host.host).as_posix() for item in reclaimed]
    oldest = [
        (mainnet, 2), (mexc, 2), (mainnet, 3), (mexc, 3), (mainnet, 4), (mexc, 4),
        (mainnet, 5), (mexc, 5), (mainnet, 6),
    ]
    expected = [
        (family.parent / f"engine.wal.{index:06d}").relative_to(host.host).as_posix()
        for family, index in oldest
    ]
    assert order == expected[: len(order)]
    assert 0 < len(order) < len(expected)
    assert {name.split("/")[2] for name in order} == {
        "liquidity-migration-engine-mainnet",
        "liquidity-migration-engine-mexc",
    }
    freed = sum(item["bytes"] for item in reclaimed)
    assert status["free_bytes"] + freed >= status["high_water_bytes"]
    assert status["free_bytes"] + freed - reclaimed[-1]["bytes"] < status["high_water_bytes"]
    assert status["reclaimed_by_class"]["wal"] == freed
    assert status["reclaimed_bytes_this_run"] == freed

    for family, index in oldest[: len(order)]:
        segment = family.parent / f"engine.wal.{index:06d}"
        assert not segment.exists()
        assert not (host.stage / str(segment.relative_to(segment.anchor))).exists()
        landed = host.remote_root / REMOTE.split(":", 1)[1] / "sealed" / str(segment.relative_to(segment.anchor))
        assert landed.is_file()
    for family, index in oldest[len(order):]:
        assert (family.parent / f"engine.wal.{index:06d}").exists()
    # The family file, the floor, the newest three and the young segment stay.
    assert mainnet.exists() and mexc.exists()
    for index in (7, 8, 9, 10):
        assert (mainnet.parent / f"engine.wal.{index:06d}").exists()
    for index in (6, 7, 8):
        assert (mexc.parent / f"engine.wal.{index:06d}").exists()
    families = {row["family"]: row for row in status["per_family"]}
    assert families[str(mainnet)]["retention_floor_segment"] == 8
    assert families[str(mainnet)]["candidates"] == 5
    assert families[str(mexc)]["candidates"] == 4
    assert sum(row["reclaimed"] for row in status["per_family"]) == len(order)


def test_the_per_run_byte_cap_stops_the_reclamation(host: Host) -> None:
    mainnet, mexc = two_families(host)
    assert host.run(*PRESSURE, "--max-wal-bytes-per-run", "5MiB") == 0
    status = host.status()
    reclaimed = [item for item in status["plan"] if item["class"] == "wal"]
    assert [Path(item["path"]).name for item in reclaimed] == ["engine.wal.000002", "engine.wal.000002"]
    freed = sum(item["bytes"] for item in reclaimed)
    assert freed <= 5 * MIB
    assert freed + 2 * MIB > 5 * MIB
    assert status["free_bytes"] + freed < status["high_water_bytes"]
    assert not (mainnet.parent / "engine.wal.000002").exists()
    assert not (mexc.parent / "engine.wal.000002").exists()
    assert (mainnet.parent / "engine.wal.000003").exists()


def test_an_unverifiable_segment_is_backlog_and_an_unlinked_stage_copy_is_not_a_candidate(
    host: Host,
) -> None:
    mainnet = host.family("mainnet", current=10, floor=9)
    host.segment(mainnet, 2, age_hours=200.0, remote="other_md5")
    host.segment(mainnet, 3, age_hours=199.0, stage="copy")
    host.segment(mainnet, 4, age_hours=198.0, remote="absent")
    host.segment(mainnet, 5, age_hours=197.0)
    for index in (6, 7, 8, 9):
        host.segment(mainnet, index, age_hours=0.5)
    assert host.run(*PRESSURE) == 0

    status = host.status()
    reasons = {row["segment"]: row["reason"] for row in status["unverified_backlog"]}
    assert reasons == {2: "md5_mismatch", 3: "stage_not_linked", 4: "remote_missing"}
    assert status["unverified_backlog_bytes"] == sum(
        freeable(mainnet.parent / f"engine.wal.{index:06d}") for index in (2, 3, 4)
    )
    for index in (2, 3, 4):
        assert (mainnet.parent / f"engine.wal.{index:06d}").exists()
    assert not (mainnet.parent / "engine.wal.000005").exists()
    assert [row["candidates"] for row in status["per_family"]] == [3]
    assert [row["verified"] for row in status["per_family"]] == [1]


def test_the_archive_copy_is_verified_before_the_receipt_and_the_receipt_before_the_deletion(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    mainnet = host.family("mainnet", current=6, floor=5)
    target = host.segment(mainnet, 2, age_hours=200.0)
    identity = target.stat()
    for index in (3, 4, 5):
        host.segment(mainnet, index, age_hours=0.5)
    seen: dict[str, list[str]] = {}
    original_append = MODULE.Ledger.append

    def append(self: Any, row: dict[str, Any]) -> None:
        seen[row["path"]] = host.rclone_calls()
        original_append(self, row)

    real_unlink = os.unlink

    def unlink(path: Any, **kwargs: Any) -> None:
        name = os.fspath(path)
        if "engine.wal.0" in str(name):
            rows = host.ledger()
            assert str(target) in {row["path"] for row in rows}, rows
        real_unlink(path, **kwargs)

    monkeypatch.setattr(MODULE.Ledger, "append", append)
    monkeypatch.setattr(os, "unlink", unlink)
    assert host.run(*PRESSURE) == 0

    calls = seen[str(target)]
    sealed = f"sealed/{target.relative_to(target.anchor)}"
    copies = [index for index, line in enumerate(calls) if line.startswith("copyto") and sealed in line]
    listings = [
        index
        for index, line in enumerate(calls)
        if line.startswith("lsjson") and "/sealed/" in line
    ]
    assert copies and listings
    assert copies[0] < listings[-1]
    row = next(item for item in host.ledger() if item["path"] == str(target))
    assert row["class"] == "wal"
    assert row["segment"] == 2
    assert row["family"] == str(mainnet)
    # check_fleet_liveness.reclaimed_wal_identities reads exactly these two.
    assert (row["st_dev"], row["st_ino"]) == (identity.st_dev, identity.st_ino)
    assert row["remote"] == f"{REMOTE}/{sealed}"
    assert row["md5"] == MODULE.file_md5(
        host.remote_root / REMOTE.split(":", 1)[1] / "sealed" / str(target.relative_to(target.anchor))
    )


def test_a_sealed_copy_that_does_not_verify_is_reported_and_kept(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUB_SEALED_CORRUPT", "1")
    mainnet = host.family("mainnet", current=6, floor=5)
    target = host.segment(mainnet, 2, age_hours=200.0)
    for index in (3, 4, 5):
        host.segment(mainnet, index, age_hours=0.5)
    assert host.run(*PRESSURE) == 1
    status = host.status()
    assert any("does not verify" in message for message in status["errors"])
    assert target.exists()
    assert host.ledger() == []
    assert not host.stamp.exists()
    assert status["reclaimed_by_class"]["wal"] == 0


def test_a_held_backup_lock_stops_every_deletion(host: Host) -> None:
    mainnet = host.family("mainnet", current=6, floor=5)
    target = host.segment(mainnet, 2, age_hours=200.0)
    for index in (3, 4, 5):
        host.segment(mainnet, index, age_hours=0.5)
    host.lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(host.lock), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        assert host.run(*PRESSURE, "--lock-timeout-s", "0.2") == 1
        assert time.monotonic() - started >= 0.2
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    status = host.status()
    assert status["lock_timeout"] is True
    assert target.exists()
    assert host.ledger() == []
    assert not host.stamp.exists()


def test_deploy_artifacts_keep_what_is_pinned_referenced_or_young(host: Host) -> None:
    host.family("mainnet", current=3, floor=2)
    host.deploy_tree()
    assert MODULE.parse_settings(host.argv()).keep_commits == (MODULE.DEFAULT_KEEP_COMMIT,)
    assert host.run(*PRESSURE, "--keep-commit", COMMITS["keep"]) == 0

    releases = host.release_dir / "releases"
    staged = host.release_dir / "staged"
    for name in ("deployed", "previous", "override", "young"):
        assert (releases / COMMITS[name]).is_dir(), name
        assert (staged / f"{COMMITS[name]}.tar.gz").is_file(), name
    assert (releases / COMMITS["keep"]).is_dir()
    for name in ("stale", "stale2"):
        assert not (releases / COMMITS[name]).exists(), name
        assert not (staged / f"{COMMITS[name]}.tar.gz").exists(), name
    assert not (staged / ".qualified.old").exists()
    assert (staged / ".qualified.new").is_dir()
    for kept in ("bin", "checkpoint-configs", "incumbent-demo", "engine.fingerprint",
                 "deployed-commit", "previous-commit"):
        assert (host.release_dir / kept).exists(), kept
    classes = {row["class"] for row in host.ledger()}
    assert classes == {"release", "staged"}
    assert all(row["md5"] is None and row["remote"] is None for row in host.ledger())
    status = host.status()
    assert status["reclaimed_by_class"]["release"] > 0
    assert status["reclaimed_by_class"]["staged"] > 0


def test_an_archive_root_is_uploaded_verified_deleted_and_its_remainder_reported(host: Host) -> None:
    host.family("mainnet", current=3, floor=2)
    tree = host.archive / "2026-09-05/evidence"
    tree.mkdir(parents=True)
    old = []
    for index, name in enumerate(("a.json", "b.json", "c.json")):
        path = tree / name
        path.write_bytes(bytes([index + 1]) * (64 * 1024))
        when = NOW - (300 - index) * 3600.0
        os.utime(path, (when, when))
        old.append(path)
    young = host.archive / "fresh.json"
    young.write_bytes(b"y" * 1024)
    os.utime(young, (NOW - 3600.0, NOW - 3600.0))
    link = host.archive / "link.json"
    link.symlink_to(old[0])

    assert host.run(*PRESSURE, "--max-upload-bytes-per-run", "150KiB") == 0

    assert not old[0].exists() and not old[1].exists()
    assert old[2].exists()
    assert young.exists()
    assert link.is_symlink()
    assert host.archive.is_dir()
    sealed = host.remote_root / REMOTE.split(":", 1)[1] / "sealed"
    for path in old[:2]:
        assert (sealed / str(path.relative_to(path.anchor))).is_file()
    rows = [row for row in host.ledger() if row["class"] == "archive"]
    assert [row["path"] for row in rows] == [str(old[0]), str(old[1])]
    assert all(row["md5"] and row["family"] is None and row["segment"] is None for row in rows)
    status = host.status()
    assert status["reclaimed_by_class"]["archive"] == sum(row["bytes"] for row in rows)
    assert status["archive_pending_bytes"] == freeable(old[2]) + freeable(young)

    assert host.run(*PRESSURE) == 0
    assert not old[2].exists()
    assert not tree.exists()
    assert not tree.parent.exists()
    assert host.archive.is_dir()
    assert young.exists()


def test_a_dry_run_plans_everything_and_mutates_nothing(host: Host, capsys: pytest.CaptureFixture[str]) -> None:
    mainnet, mexc = two_families(host)
    host.deploy_tree()
    tree = host.archive / "evidence"
    tree.mkdir(parents=True)
    quarantined = tree / "old.json"
    quarantined.write_bytes(b"q" * 4096)
    os.utime(quarantined, (NOW - 400 * 3600.0, NOW - 400 * 3600.0))

    assert host.run(*PRESSURE, "--dry-run", "--json") == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["dry_run"] is True
    kinds = {item["class"] for item in plan["plan"]}
    assert kinds == {"wal", "archive", "release", "staged"}
    assert str(quarantined) in {item["path"] for item in plan["plan"]}
    assert str(mainnet.parent / "engine.wal.000002") in {item["path"] for item in plan["plan"]}

    assert quarantined.exists()
    assert (mainnet.parent / "engine.wal.000002").exists()
    assert (mexc.parent / "engine.wal.000002").exists()
    assert (host.release_dir / "releases" / COMMITS["stale"]).is_dir()
    assert not host.stamp.exists()
    assert not host.state.exists()
    assert all(not line.startswith("copyto") for line in host.rclone_calls())


def test_one_family_without_a_retention_floor_is_skipped_and_the_run_fails(host: Host) -> None:
    mainnet, mexc = two_families(host)
    host.break_retention(mexc, mode="missing")
    assert host.run(*PRESSURE) == 1

    status = host.status()
    assert any(str(mexc) in message and "wal-retention" in message for message in status["errors"])
    rows = {row["family"]: row for row in status["per_family"]}
    assert rows[str(mexc)]["retention_floor_segment"] is None
    assert rows[str(mexc)]["candidates"] == 0
    assert rows[str(mainnet)]["retention_floor_segment"] == 8
    assert rows[str(mainnet)]["reclaimed"] > 0
    assert not (mainnet.parent / "engine.wal.000002").exists()
    for index in (2, 3, 4, 5, 6):
        assert (mexc.parent / f"engine.wal.{index:06d}").exists()
    assert not host.stamp.exists()
    assert host.ledger()


@pytest.mark.parametrize("mode", ["malformed", "absent"])
def test_a_floor_the_tool_cannot_state_reclaims_nothing(host: Host, mode: str) -> None:
    mainnet = host.family("mainnet", current=6, floor=5)
    target = host.segment(mainnet, 2, age_hours=200.0)
    for index in (3, 4, 5):
        host.segment(mainnet, index, age_hours=0.5)
    if mode == "malformed":
        host.break_retention(mainnet, mode="malformed")
        assert host.run(*PRESSURE) == 1
    else:
        # The tool is not on this host at all: a failed step, not a traceback.
        assert host.run(*PRESSURE, "--engine-tools", str(host.tmp / "nowhere/engine-tools")) == 1
    status = host.status()
    assert any("wal-retention" in message for message in status["errors"])
    assert target.exists()
    assert host.ledger() == []
    assert not host.stamp.exists()
    assert status["per_family"] == [
        {
            "family": str(mainnet),
            "current_segment": None,
            "retention_floor_segment": None,
            "candidates": 0,
            "verified": 0,
            "reclaimed": 0,
        }
    ]


def test_growth_and_runway_come_from_the_recorded_samples(host: Host) -> None:
    host.family("mainnet", current=3, floor=2)
    host.state.mkdir(parents=True, exist_ok=True)
    rows = [
        {"measured_at_s": NOW - 30 * 3600.0, "used_bytes": 1_000_000, "reclaimed_bytes": 0},
        {"measured_at_s": NOW - 12 * 3600.0, "used_bytes": 4_000_000, "reclaimed_bytes": 1_000_000},
        {"measured_at_s": NOW - 2 * 3600.0, "used_bytes": 6_000_000, "reclaimed_bytes": 0},
    ]
    (host.state / "samples.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    capacity = 100 * GIB
    free = 40 * GIB
    stats = statvfs_for(capacity, free)
    assert MODULE.main(host.argv(), statvfs=lambda _: stats, now=lambda: NOW) == 0

    status = host.status()
    span = 10 * 3600.0
    growth = (6_000_000 - 4_000_000 + 1_000_000) / span
    assert status["growth_bytes_per_s"] == pytest.approx(growth)
    reserve = int(0.12 * capacity)
    low_water = reserve + 6 * GIB
    assert status["reserve_bytes"] == reserve
    assert status["low_water_bytes"] == low_water
    assert status["high_water_bytes"] == low_water + int(growth * 2 * 86_400.0)
    assert status["estimated_runway_s"] == pytest.approx((free - low_water) / growth)
    assert status["runway_with_verified_history_s"] == pytest.approx(
        (free - low_water + status["retained_verified_wal_bytes"]) / growth
    )
    assert [row["measured_at_s"] for row in _samples(host)][-1] == NOW


def test_the_low_water_clears_the_tape_recorders_free_floor(host: Host) -> None:
    # A recorder under `[storage].min_free_disk_gb` counts every frame and writes
    # none; a sealed segment below the engine's floor has a verified copy. The
    # WAL yields first: low water is a writer headroom above the highest
    # recorder floor, not only above the reserve.
    two_families(host)
    host.capture_config("bybit-linear", "[storage]\nmin_free_disk_gb = 25\n")
    host.capture_config("binance-usdm", "[storage]\nmax_disk_gb = 18\n")  # the recorder's default, 12
    host.capture_config("spare", "[storage]\nmin_free_disk_gb = 18\n")
    settings = MODULE.parse_settings(host.argv(*PRESSURE))
    assert MODULE.tape_free_floor_bytes(settings.tape_floor_configs) == (25 * GIB, [])
    capacity = 118 * GIB
    budget = MODULE.measure_budget(statvfs_for(capacity, 30 * GIB), [], NOW, settings, 25 * GIB)
    assert (budget.reserve_bytes, budget.tape_floor_bytes) == (6 * MIB, 25 * GIB)
    assert budget.low_water_bytes == 25 * GIB + 2 * MIB
    assert budget.high_water_bytes == budget.low_water_bytes + 4 * MIB

    # Above the recorders' floor nothing is reclaimed; under it, far above the
    # reserve, verified history goes.
    assert host.run(*PRESSURE, capacity=capacity, free=26 * GIB) == 0
    assert host.status()["reclaimed_by_class"]["wal"] == 0
    assert host.run(*PRESSURE, capacity=capacity, free=24 * GIB) == 0
    status = host.status()
    assert (status["tape_floor_bytes"], status["low_water_bytes"]) == (25 * GIB, 25 * GIB + 2 * MIB)
    assert status["reclaimed_by_class"]["wal"] > 0


def test_an_unreadable_capture_config_is_a_fault_and_the_readable_floors_still_hold(host: Host) -> None:
    two_families(host)
    host.capture_config("bybit-linear", "[storage]\nmin_free_disk_gb = 25\n")
    host.capture_config("broken", "[storage\n")
    host.capture_config("negative", "[storage]\nmin_free_disk_gb = -1\n")
    assert host.run(*PRESSURE, capacity=118 * GIB, free=24 * GIB) == 1
    status = host.status()
    assert sorted(status["errors"]) == sorted(
        [error for error in status["errors"] if error.startswith("tape floor ")]
    )
    assert {error.split(": ", 1)[0] for error in status["errors"]} == {
        f"tape floor {host.capture / 'broken.toml'}",
        f"tape floor {host.capture / 'negative.toml'}",
    }
    assert status["tape_floor_bytes"] == 25 * GIB
    assert status["reclaimed_by_class"]["wal"] > 0
    assert not host.stamp.exists()


def test_the_default_family_list_is_one_engine_wal_per_realm() -> None:
    families = MODULE.default_wal_families()
    assert families == tuple(row.engine_wal for row in realm_table())
    assert len(families) == 4
    for family in families:
        assert Path(family).is_absolute()
        assert Path(family).name == "engine.wal"
        assert family.startswith("/var/lib/liquidity-migration-engine")


def test_the_flagless_defaults_are_the_ones_the_deployed_unit_relies_on(host: Host) -> None:
    # liquidity-migration-storage-reclaim.service passes no flag at all.
    settings = MODULE.parse_settings([])
    assert settings.filesystem == "/var/lib/liquidity-migration"
    assert settings.state_dir == Path("/var/lib/liquidity-migration/storage-reclaim")
    assert settings.stamp_file == Path(
        "/var/lib/liquidity-migration/receipts/storage-reclaim.last-success"
    )
    assert settings.remote == "gdrive:LiquidityMigration/engine-state"
    assert settings.rclone_config == Path("/var/lib/liquidity-migration/backup/rclone.conf")
    assert settings.rclone_bin == Path("/usr/bin/rclone")
    assert settings.engine_tools == Path("/opt/liquidity-migration-engine/bin/engine-tools")
    assert settings.backup_lock == Path("/var/lib/liquidity-migration/backup/backup.lock")
    assert settings.backup_stage == Path("/var/lib/liquidity-migration/backup/stage")
    assert settings.archive_roots == (Path("/var/lib/liquidity-migration-wal-quarantine"),)
    assert settings.tape_floor_configs == (Path(MODULE.DEFAULT_TAPE_FLOOR_CONFIG),)
    # Both recorders hold min_free_disk_gb = 12; the deployed low water clears it.
    assert MODULE.tape_free_floor_bytes(settings.tape_floor_configs) == (12 * GIB, [])
    assert settings.release_dir == Path("/opt/liquidity-migration-engine")
    assert settings.systemd_dir == Path("/etc/systemd/system")
    assert settings.wal_families == tuple(Path(row.engine_wal) for row in realm_table())
    assert settings.apt_clean is True
    assert settings.dry_run is False and settings.json_output is False
    assert (settings.reserve_fraction, settings.reserve_floor_gib) == (0.12, 8.0)
    assert (settings.writer_headroom_gib, settings.high_water_days) == (6.0, 2.0)
    assert (settings.high_water_default_gib, settings.archive_min_age_hours) == (10.0, 24.0)
    assert (settings.wal_min_age_hours, settings.wal_keep_newest) == (48.0, 3)
    assert (settings.release_age_days, settings.lock_timeout_s) == (1.0, 300.0)
    assert settings.max_wal_bytes_per_run == 12 * GIB
    assert settings.max_upload_bytes_per_run == 2 * GIB


def test_environment_overrides_use_the_reclaim_prefix_and_flags_win(
    host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The unit sets RECLAIM_RCLONE_CONFIG; everything else must work the same way.
    monkeypatch.setenv("RECLAIM_RCLONE_CONFIG", "/var/lib/liquidity-migration/backup/other.conf")
    monkeypatch.setenv("RECLAIM_REMOTE", "gdrive:Other/engine-state")
    monkeypatch.setenv("RECLAIM_MAX_WAL_BYTES_PER_RUN", "3GiB")
    monkeypatch.setenv("RECLAIM_WAL_KEEP_NEWEST", "5")
    monkeypatch.setenv("RECLAIM_APT_CLEAN", "0")
    monkeypatch.setenv("RECLAIM_WAL_FAMILY", "/var/lib/one/engine.wal /var/lib/two/engine.wal")
    monkeypatch.setenv("RECLAIM_ARCHIVE_ROOT", "/var/lib/quarantine-a /var/lib/quarantine-b")
    monkeypatch.setenv("RECLAIM_TAPE_FLOOR_CONFIG", "/etc/capture/a.toml /etc/capture")
    settings = MODULE.parse_settings([])
    assert settings.rclone_config == Path("/var/lib/liquidity-migration/backup/other.conf")
    assert settings.remote == "gdrive:Other/engine-state"
    assert settings.max_wal_bytes_per_run == 3 * GIB
    assert settings.wal_keep_newest == 5
    assert settings.apt_clean is False
    assert settings.wal_families == (Path("/var/lib/one/engine.wal"), Path("/var/lib/two/engine.wal"))
    assert settings.archive_roots == (Path("/var/lib/quarantine-a"), Path("/var/lib/quarantine-b"))
    assert settings.tape_floor_configs == (Path("/etc/capture/a.toml"), Path("/etc/capture"))
    overridden = MODULE.parse_settings(
        ["--max-wal-bytes-per-run", "1GiB", "--apt-clean", "--wal-family", "/var/lib/three/engine.wal"]
    )
    assert overridden.max_wal_bytes_per_run == GIB
    assert overridden.apt_clean is True
    assert overridden.wal_families == (Path("/var/lib/three/engine.wal"),)


def test_bad_arguments_exit_two(host: Host) -> None:
    host.family("mainnet", current=3, floor=2)
    for extra in (
        ("--remote", "no-colon-here"),
        ("--state-dir", "relative/state"),
        ("--backup-stage", "relative/stage"),
        ("--keep-commit", "not-a-commit"),
        ("--max-wal-bytes-per-run", "12 furlongs"),
        ("--wal-family", "/var/lib/liquidity-migration-engine/heartbeat.json"),
        ("--tape-floor-config", "relative/capture.toml"),
    ):
        with pytest.raises(SystemExit) as failure:
            host.run(*extra)
        assert failure.value.code == 2, extra


def test_a_family_with_no_log_yet_is_nothing_to_reclaim(host: Host) -> None:
    # A realm that has never run has an empty state directory and no engine.wal,
    # and the retention tool refuses such a family. That is not a fault.
    mainnet, _ = two_families(host)
    unborn = host.host / "var/lib/liquidity-migration-engine-hyperliquid"
    unborn.mkdir(parents=True)
    family = unborn / "engine.wal"
    host.families.append(family)
    assert host.run(*PRESSURE) == 0

    status = host.status()
    assert status["errors"] == []
    rows = {row["family"]: row for row in status["per_family"]}
    assert rows[str(family)]["retention_floor_segment"] is None
    assert rows[str(family)]["candidates"] == 0
    assert rows[str(mainnet)]["reclaimed"] > 0
    assert host.stamp.exists()


def test_every_external_command_has_a_deadline_and_a_hang_is_a_failed_step(host: Host, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    seen: list[float | None] = []

    def run(args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"), output=b"partial")

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    reclaimer = MODULE.Reclaimer(MODULE.parse_settings(host.argv()), lambda _: statvfs_for(GIB, MIB), lambda: NOW)
    result = reclaimer._run(["/usr/bin/rclone", "lsjson", "gdrive:x"])
    assert seen == [MODULE.SUBPROCESS_TIMEOUT_SECONDS]
    assert result.returncode == 124
    assert result.stdout == "partial"
    assert "no answer within" in result.stderr
    assert reclaimer.retention_floor(Path("/var/lib/liquidity-migration-engine/engine.wal")) is None
    assert any("exit 124" in error and "no answer within" in error for error in reclaimer.errors), reclaimer.errors
