"""The old recorder path still starts the recorder, which lives in `market_tape`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "research" / "capture_bybit_forward.py"


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
    assert "--root" in done.stdout


def test_the_old_module_path_still_imports_a_main() -> None:
    from scripts.research.capture_bybit_forward import main

    assert callable(main)


def test_the_command_line_defaults_are_the_storage_settings_defaults() -> None:
    from market_tape.config import StorageSettings
    from scripts.research.capture_bybit_forward import parser

    args = parser().parse_args(["--root", "/tmp/tape", "--symbols", "BTCUSDT"])
    defaults = StorageSettings()
    assert args.segment_max_mb == defaults.segment_max_mb
    assert args.fsync_every_records == defaults.fsync_every_records
    assert args.retention_days == defaults.retention_days
    assert args.max_disk_gb == defaults.max_disk_gb
    assert args.min_free_disk_gb == defaults.min_free_disk_gb
    assert args.queue_frames == defaults.queue_frames
    assert args.status_interval_seconds == defaults.status_interval_seconds


def test_the_wide_universe_builds_a_config_the_recorder_accepts() -> None:
    from scripts.research.capture_bybit_forward import config_from_args, parser

    args = parser().parse_args(["--root", "/tmp/tape", "--symbols", "BTCUSDT", "--wide-universe", "linear-usdt"])
    config = config_from_args(args)
    assert [tier.name for tier in config.tiers] == ["deep", "crowded", "wide"]
    assert config.tiers[1].universe.kind == "funding_below"
    assert config.tiers[1].universe.sticky_hours == 48.0
