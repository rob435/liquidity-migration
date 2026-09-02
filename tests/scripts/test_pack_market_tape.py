"""The old packer path still runs the packer, which lives in `market_tape.pack`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "runtime" / "pack_market_tape.py"


def test_the_old_command_line_still_answers() -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stderr
    assert "market_tape" in done.stdout
    assert "--state-dir" in done.stdout


def test_the_old_module_path_still_imports_a_main() -> None:
    from scripts.runtime import pack_market_tape

    assert callable(pack_market_tape.main)
