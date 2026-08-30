"""The backup and chaos-drill scripts, and the unit wiring that runs them."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts" / "runtime" / "backup_state.sh"
DRILL = ROOT / "scripts" / "runtime" / "chaos_drill.sh"
WRAPPER = ROOT / "scripts" / "run_authorized_runtime.sh"
SYSTEMD = ROOT / "deploy" / "systemd"


def test_the_drill_is_hardwired_to_the_demo_engine() -> None:
    # The one property that must survive every future edit: no spelling of
    # the funded unit anywhere in the drill. A drill that can reach mainnet
    # is not a drill.
    text = DRILL.read_text(encoding="utf-8")
    assert 'UNIT="liquidity-migration-engine.service"' in text
    assert "mainnet" not in text.replace("never touches mainnet", "").lower().replace(
        "not for rehearsing", ""
    ), "the funded unit's name has no business in this file"
    assert "engine-mainnet" not in text
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text


def test_an_unconfigured_backup_is_a_note_and_a_clean_exit() -> None:
    # Fail-open like the Telegram sender: an owner who has not set a
    # destination gets exit 0 and a sentence, never a failed unit.
    env = {k: v for k, v in os.environ.items() if not k.startswith("BACKUP_")}
    done = subprocess.run(
        ["bash", str(BACKUP)], env=env, capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    assert "not set" in done.stdout


def test_a_configured_backup_requires_its_stamp_and_sources() -> None:
    # Half-configured must refuse loudly, not copy to nowhere or copy and
    # leave the watchdog's stamp unwritten forever.
    env = {k: v for k, v in os.environ.items() if not k.startswith("BACKUP_")}
    env["BACKUP_RSYNC_DEST"] = "user@backup.invalid:liquidity/"
    done = subprocess.run(
        ["bash", str(BACKUP)], env=env, capture_output=True, text=True, timeout=30
    )
    assert done.returncode != 0
    assert "BACKUP_STAMP_FILE" in done.stderr


def test_both_new_units_dispatch_through_the_committed_wrapper() -> None:
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert "liquidity-migration-backup.service:main" in wrapper
    assert "liquidity-migration-chaos-drill.service:main" in wrapper
    assert "backup_state.sh" in wrapper
    assert "chaos_drill.sh" in wrapper


def test_the_drill_timer_is_deliberately_not_persistent() -> None:
    # A box booting after an outage has just had its recovery exercised for
    # real; Persistent=true would greet it with another kill.
    timer = (SYSTEMD / "liquidity-migration-chaos-drill.timer").read_text(encoding="utf-8")
    assert "Persistent=true" not in timer
    assert "OnCalendar=Sun" in timer
    backup_timer = (SYSTEMD / "liquidity-migration-backup.timer").read_text(encoding="utf-8")
    assert "Persistent=true" in backup_timer, "a missed nightly backup runs on boot instead"
