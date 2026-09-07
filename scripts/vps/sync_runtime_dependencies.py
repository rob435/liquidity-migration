#!/usr/bin/env python3
"""Remove distributions not declared by the host runtime lock."""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sync(lock: Path) -> None:
    allowed = {"pip", "setuptools", "wheel"}
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s;]+", line):
            raise ValueError(f"runtime lock requires exact pins: {line!r}")
        allowed.add(normalize(line.split("==", 1)[0]))
    unwanted = sorted({
        distribution.metadata["Name"]
        for distribution in importlib.metadata.distributions()
        if normalize(distribution.metadata["Name"]) not in allowed
    })
    if unwanted:
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "--yes", *unwanted], check=True)
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)


if __name__ == "__main__":
    sync(Path(sys.argv[1]))
