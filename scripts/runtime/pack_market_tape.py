#!/usr/bin/env python3
"""Pack each finished hour of the market tape into one archive and upload it to Google Drive.

The recorder writes one compressed file per symbol per UTC hour under
<root>/<day>/<HH>/<SYMBOL>/ plus the daily snapshots under <day>/<HH>/_meta/.
That is hundreds of files an hour, and a cloud folder full of them is a mess
nobody can browse. This job takes every hour that has finished (its end is
more than --grace-seconds ago and nothing under it is still open or
uncompressed), writes one uncompressed tar of its already-compressed files
with a MANIFEST.json first, uploads it as
<remote>/<YYYY>/<MM>/<DD>/<day>T<HH>Z.tar, reads the Drive's own hash back
to prove the bytes landed, and records the hour in a local ledger so it is
never packed twice. A day folder in the older daily layout
(<day>/<SYMBOL>/...) is packed once, whole, as <day>.legacy.tar once the day
has ended.

The local files stay for the recorder's own retention window; the archive on
the Drive is the copy that lasts.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HOUR_RE = re.compile(r"^\d{2}$")


@dataclass(frozen=True)
class Candidate:
    """One archive to build: a finished hour, or a whole legacy day."""

    name: str
    day: str
    hour: str | None
    directories: tuple[Path, ...]

    @property
    def remote_name(self) -> str:
        year, month, day = self.day.split("-")
        return f"{year}/{month}/{day}/{self.name}.tar"


def _raw_files_under(directory: Path) -> bool:
    for path in directory.rglob("*"):
        if path.is_file() and not path.name.endswith(".zst"):
            return True
    return False


def _has_archives(directory: Path) -> bool:
    return any(path.is_file() and path.name.endswith(".zst") for path in directory.rglob("*"))


def finished_candidates(root: Path, *, now: float, grace_seconds: float) -> list[Candidate]:
    """Hours (and legacy days) whose files are all closed and whose time has passed."""

    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    today = moment.date().isoformat()
    candidates: list[Candidate] = []
    for day_dir in sorted(path for path in root.iterdir() if path.is_dir() and DAY_RE.match(path.name)):
        day = day_dir.name
        legacy_dirs: list[Path] = []
        for child in sorted(path for path in day_dir.iterdir() if path.is_dir()):
            if HOUR_RE.match(child.name):
                hour_end = datetime.fromisoformat(day).replace(tzinfo=timezone.utc) + timedelta(hours=int(child.name) + 1)
                if now < hour_end.timestamp() + grace_seconds:
                    continue
                if _raw_files_under(child) or not _has_archives(child):
                    continue
                candidates.append(Candidate(f"{day}T{child.name}Z", day, child.name, (child,)))
            elif child.name != "_meta":
                legacy_dirs.append(child)
        if legacy_dirs and day < today:
            if any(_raw_files_under(path) for path in legacy_dirs):
                continue
            if any(_has_archives(path) for path in legacy_dirs):
                candidates.append(Candidate(f"{day}.legacy", day, None, tuple(legacy_dirs)))
    return candidates


def load_ledger(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rows[str(row["name"])] = row
    return rows


def append_ledger(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_capture_manifest(path: Path) -> dict[str, dict[str, Any]]:
    """The recorder's own receipts, by relative path, for row counts and time spans."""

    rows: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind") in {"segment_compressed", "snapshot_compressed"} and row.get("path"):
                rows[str(row["path"])] = row
    return rows


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _md5(path: Path) -> str:
    hasher = hashlib.md5()  # noqa: S324 - Drive reports MD5; this only compares against it
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def build_archive(candidate: Candidate, root: Path, staging: Path, receipts: dict[str, dict[str, Any]]) -> tuple[Path, dict[str, Any]]:
    """Write <staging>/<name>.tar with MANIFEST.json first; return its path and manifest."""

    files: list[dict[str, Any]] = []
    members: list[tuple[Path, str]] = []
    for directory in candidate.directories:
        for path in sorted(p for p in directory.rglob("*") if p.is_file() and p.name.endswith(".zst")):
            relative_to_root = str(path.relative_to(root))
            # An hour archive implies its hour directory; a legacy day archive
            # keeps each symbol directory as the member's first path element.
            arcname = str(path.relative_to(directory if candidate.hour is not None else directory.parent))
            receipt = receipts.get(relative_to_root, {})
            files.append(
                {
                    "path": arcname,
                    "bytes": path.stat().st_size,
                    "sha256": _sha256(path),
                    "records": receipt.get("records"),
                    "first_receive_ns": receipt.get("first_receive_ns"),
                    "last_receive_ns": receipt.get("last_receive_ns"),
                    "symbol": receipt.get("symbol"),
                    "snapshot": receipt.get("snapshot"),
                }
            )
            members.append((path, arcname))
    manifest = {
        "kind": "market_tape_hour" if candidate.hour is not None else "market_tape_legacy_day",
        "name": candidate.name,
        "day": candidate.day,
        "hour": candidate.hour,
        "created_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": files,
        "file_count": len(files),
        "bytes": sum(int(row["bytes"]) for row in files),
        "symbols": sorted({row["symbol"] for row in files if row.get("symbol")}),
    }
    staging.mkdir(parents=True, exist_ok=True)
    output = staging / f"{candidate.name}.tar"
    temporary = staging / f".{candidate.name}.tar.tmp"
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    with tarfile.open(temporary, "w", format=tarfile.PAX_FORMAT) as archive:
        info = tarfile.TarInfo("MANIFEST.json")
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        info.mode = 0o644
        archive.addfile(info, fileobj=_Bytes(manifest_bytes))
        for path, arcname in members:
            info = archive.gettarinfo(str(path), arcname=arcname)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with path.open("rb") as handle:
                archive.addfile(info, fileobj=handle)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, output)
    return output, manifest


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._offset
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class Rclone:
    def __init__(self, binary: str, config: Path) -> None:
        self.binary = binary
        self.config = config

    def run(self, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.binary, *args, "--config", str(self.config)],
            check=True,
            text=True,
            capture_output=capture,
        )

    def upload(self, local: Path, remote_path: str) -> None:
        self.run(
            "copyto",
            str(local),
            remote_path,
            "--drive-chunk-size",
            "32M",
            "--retries",
            "5",
            "--low-level-retries",
            "10",
        )

    def remote_md5_and_size(self, remote_path: str) -> tuple[str | None, int | None]:
        parent, _, name = remote_path.rpartition("/")
        done = self.run("lsjson", parent, "--hash", "--files-only", capture=True)
        for row in json.loads(done.stdout or "[]"):
            if row.get("Name") == name:
                hashes = row.get("Hashes") or {}
                return hashes.get("md5") or hashes.get("MD5"), row.get("Size")
        return None, None

    def free_bytes(self, remote: str) -> int | None:
        try:
            done = self.run("about", remote.split(":", 1)[0] + ":", "--json", capture=True)
            value = json.loads(done.stdout or "{}").get("free")
            return int(value) if isinstance(value, int) else None
        except (subprocess.CalledProcessError, ValueError):
            return None


def seed_config(config: Path, seed: Path | None) -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    if seed is not None:
        if not seed.exists():
            raise SystemExit(f"rclone config seed is missing: {seed}")
        if not config.exists() or seed.stat().st_mtime > config.stat().st_mtime:
            temporary = config.with_name(f".{config.name}.seed.{os.getpid()}")
            shutil.copyfile(seed, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, config)
    if not config.exists():
        raise SystemExit(f"rclone config is missing: {config}")


def write_stamp(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={'' if value is None else value}\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def ship(candidates: Iterable[Candidate], *, root: Path, remote: str, staging: Path, ledger_path: Path, rclone: Rclone) -> list[dict[str, Any]]:
    receipts = load_capture_manifest(root / "manifest.jsonl")
    shipped: list[dict[str, Any]] = []
    for candidate in candidates:
        archive, manifest = build_archive(candidate, root, staging, receipts)
        try:
            remote_path = f"{remote}/{candidate.remote_name}"
            local_md5 = _md5(archive)
            size = archive.stat().st_size
            rclone.upload(archive, remote_path)
            remote_md5, remote_size = rclone.remote_md5_and_size(remote_path)
            if remote_size != size or (remote_md5 is not None and remote_md5.lower() != local_md5):
                raise RuntimeError(
                    f"{candidate.name}: the Drive holds size={remote_size} md5={remote_md5}, "
                    f"not the uploaded size={size} md5={local_md5}"
                )
            row = {
                "name": candidate.name,
                "remote_path": remote_path,
                "bytes": size,
                "md5": local_md5,
                "file_count": manifest["file_count"],
                "uploaded_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            append_ledger(ledger_path, row)
            shipped.append(row)
            print(f"market tape: shipped {candidate.name} files={manifest['file_count']} bytes={size} -> {remote_path}")
        finally:
            archive.unlink(missing_ok=True)
    return shipped


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, required=True, help="the recorder's root directory")
    parser.add_argument("--remote", required=True, help="rclone destination, remote:path")
    parser.add_argument("--state-dir", type=Path, required=True, help="ledger, lock, staging, and the rclone config copy")
    parser.add_argument("--stamp-file", type=Path, required=True, help="receipt written after a fully successful run")
    parser.add_argument("--grace-seconds", type=float, default=300.0, help="how long after an hour ends before it is packed")
    parser.add_argument("--rclone", default=os.environ.get("RCLONE_BIN") or "/usr/bin/rclone")
    parser.add_argument("--config", type=Path, default=Path(os.environ.get("RCLONE_CONFIG") or "/etc/liquidity-migration/rclone.conf"))
    parser.add_argument("--config-seed", type=Path, default=Path(os.environ["RCLONE_CONFIG_SEED"]) if os.environ.get("RCLONE_CONFIG_SEED") else None)
    parser.add_argument("--now", type=float, default=None, help="override the clock (tests)")
    parser.add_argument("--dry-run", action="store_true", help="list what would be packed and stop")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if ":" not in args.remote:
        print("market tape: --remote must be an rclone remote (remote:path)", file=sys.stderr)
        return 2
    root = args.root.resolve()
    if not root.is_dir():
        print(f"market tape: capture root is missing: {root}", file=sys.stderr)
        return 2
    now = args.now if args.now is not None else time.time()
    candidates = finished_candidates(root, now=now, grace_seconds=args.grace_seconds)
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = args.state_dir / "uploaded-tapes.jsonl"
    ledger = load_ledger(ledger_path)
    pending = [candidate for candidate in candidates if candidate.name not in ledger]
    if args.dry_run:
        for candidate in pending:
            print(f"would pack {candidate.name} -> {candidate.remote_name}")
        print(f"market tape: {len(pending)} pending, {len(ledger)} already shipped")
        return 0
    with (args.state_dir / "upload.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("market tape: another run owns the upload lock")
            return 0
        if not shutil.which(args.rclone) and not os.access(args.rclone, os.X_OK):
            print(f"market tape: rclone is not executable: {args.rclone}", file=sys.stderr)
            return 2
        seed_config(args.config, args.config_seed)
        rclone = Rclone(args.rclone, args.config)
        remote = args.remote.rstrip("/")
        shipped = ship(
            pending,
            root=root,
            remote=remote,
            staging=args.state_dir / "staging",
            ledger_path=ledger_path,
            rclone=rclone,
        )
        free = rclone.free_bytes(remote)
        write_stamp(
            args.stamp_file,
            {
                "uploaded_at": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "archives": ",".join(row["name"] for row in shipped) or "none",
                "file_count": sum(int(row["file_count"]) for row in shipped),
                "bytes": sum(int(row["bytes"]) for row in shipped),
                "destination": remote,
                "remote_free_bytes": free,
            },
        )
    print(f"market tape: shipped {len(shipped)} archives to {remote}; {len(pending) - len(shipped)} left")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
