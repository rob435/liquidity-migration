#!/usr/bin/env python3
"""Share immutable rotated WALs after the staged backup has passed its remote check."""

from __future__ import annotations

import argparse
import errno
import filecmp
import os
import re
import uuid
from pathlib import Path


def link_sealed(stage: Path, sources: list[Path]) -> tuple[int, int, int]:
    linked = 0
    released = 0
    unlinkable = 0
    for directory in sources:
        if not directory.is_dir() or directory.is_symlink():
            continue
        segments = []
        for path in directory.iterdir():
            match = re.fullmatch(r"engine\.wal\.(\d+)", path.name)
            if match and path.is_file() and not path.is_symlink():
                segments.append((int(match[1]), path))
        # The highest numbered segment may still be growing, even if rsync
        # caught it between appends. Every lower segment is sealed by the WAL.
        for _, source in sorted(segments)[:-1]:
            destination = stage / source.relative_to(source.anchor)
            if not destination.is_file() or destination.is_symlink():
                continue
            before = source.stat()
            staged = destination.stat()
            if before.st_dev != staged.st_dev or before.st_ino == staged.st_ino:
                continue
            if not filecmp.cmp(source, destination, shallow=False):
                continue
            temporary = destination.with_name(f".{destination.name}.link-{uuid.uuid4().hex}")
            try:
                os.link(source, temporary)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # Hard links require one mount even when device IDs match.
                # The verified remote backup remains valid without this link.
                unlinkable += 1
                break
            try:
                after = source.stat()
                if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns
                ):
                    continue
                os.replace(temporary, destination)
                linked += 1
                if staged.st_nlink == 1:
                    released += staged.st_blocks * 512
            finally:
                temporary.unlink(missing_ok=True)
    return linked, released, unlinkable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("sources", nargs="+", type=Path)
    args = parser.parse_args()
    linked, released, unlinkable = link_sealed(args.stage, args.sources)
    print(
        f"backup: sealed WAL links={linked} released_stage_bytes={released} "
        f"unlinkable_roots={unlinkable}"
    )


if __name__ == "__main__":
    main()
