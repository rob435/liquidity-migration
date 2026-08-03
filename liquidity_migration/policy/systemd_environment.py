"""Read private systemd EnvironmentFiles as data, never as shell programs."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from liquidity_migration.core.artifact_snapshot import read_stable_file


_KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*")
_MAX_FILE_BYTES = 1024 * 1024


def parse_systemd_environment_bytes(data: bytes, *, label: str) -> dict[str, str]:
    """Parse the strict KEY=VALUE subset used by private runtime configs."""

    if len(data) > _MAX_FILE_BYTES:
        raise ValueError(f"{label} exceeds {_MAX_FILE_BYTES} bytes")
    if b"\0" in data or b"\r" in data:
        raise ValueError(f"{label} contains a forbidden control byte")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, raw_value = line.partition("=")
        if separator != "=" or not _KEY_PATTERN.fullmatch(key):
            raise ValueError(
                f"{label} line {line_number} is not a strict KEY=VALUE assignment"
            )
        if key in values:
            raise ValueError(f"{label} repeats {key}")
        if "\\" in raw_value:
            raise ValueError(
                f"{label} line {line_number} uses unsupported escape syntax"
            )
        try:
            parsed = shlex.split(raw_value, comments=False, posix=True)
        except ValueError as exc:
            raise ValueError(f"{label} line {line_number} has malformed quoting") from exc
        if len(parsed) > 1:
            raise ValueError(f"{label} line {line_number} has an ambiguous value")
        values[key] = "" if not parsed else parsed[0]
    return values


def load_private_systemd_environment(path: str | Path) -> dict[str, str]:
    """Read one owner-only, stable, regular EnvironmentFile snapshot."""

    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("private systemd environment path must be absolute")
    snapshot = read_stable_file(
        candidate,
        label="private systemd environment",
        reject_empty=True,
        require_mode=0o600,
        require_owner=True,
    )
    return parse_systemd_environment_bytes(
        snapshot.data,
        label=f"private systemd environment {snapshot.path}",
    )


def selected_environment_payload(
    values: Mapping[str, str],
    *,
    names: Sequence[str],
) -> bytes:
    """Encode selected present values as NUL-delimited key/value pairs."""

    if not names or len(set(names)) != len(names):
        raise ValueError("selected environment names must be nonempty and distinct")
    if any(not _KEY_PATTERN.fullmatch(name) for name in names):
        raise ValueError("selected environment name is invalid")
    payload = bytearray()
    for name in names:
        if name not in values:
            continue
        value = values[name]
        if not isinstance(value, str) or "\0" in value:
            raise ValueError(f"selected environment value for {name} is invalid")
        payload.extend(name.encode("ascii"))
        payload.append(0)
        payload.extend(value.encode("utf-8"))
        payload.append(0)
    return bytes(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True)
    parser.add_argument("--name", action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        values = load_private_systemd_environment(args.path)
        payload = selected_environment_payload(values, names=args.name)
        written = sys.stdout.buffer.write(payload)
        if written != len(payload):
            raise OSError("private environment transfer made a partial write")
        sys.stdout.buffer.flush()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"private systemd environment error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "load_private_systemd_environment",
    "parse_systemd_environment_bytes",
    "selected_environment_payload",
]
