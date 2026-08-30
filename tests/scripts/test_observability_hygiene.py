"""The backup and chaos-drill scripts, and the unit wiring that runs them."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
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
    assert 'UNIT="$(lm_owner_unit demo)"' in text
    assert "lm_validate_fleet_manifest" in text
    assert "mainnet" not in text.replace("never touches mainnet", "").lower().replace(
        "not for rehearsing", ""
    ), "the funded unit's name has no business in this file"
    assert "engine-mainnet" not in text
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text


def test_the_drill_accepts_only_a_fresh_exact_process_heartbeat(tmp_path: Path) -> None:
    text = DRILL.read_text(encoding="utf-8")
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY", text, re.DOTALL)
    validator = next(block for block in blocks if "def exact_int" in block)
    now_ms = time.time_ns() // 1_000_000
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "account_observed_wall_ts_ms": now_ms,
                "account_user_id": "555899665",
                "may_open": True,
                "mode": "live",
                "pid": 321,
                "realm": "demo",
                "venue": "bybit",
                "wall_ts_ms": now_ms,
            }
        ),
        encoding="utf-8",
    )
    proc_root = tmp_path / "proc"
    (proc_root / "321").mkdir(parents=True)
    (proc_root / "321" / "cgroup").write_text(
        "0::/system.slice/liquidity-migration-engine.service\n", encoding="utf-8"
    )

    argv = [
        sys.executable,
        "-",
        str(heartbeat),
        "555899665",
        "bybit",
        "demo",
        "/system.slice/liquidity-migration-engine.service",
        str(now_ms - 1),
        "0",
        str(proc_root),
    ]
    accepted = subprocess.run(
        argv, input=validator, text=True, capture_output=True, check=False
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "321 clean"

    argv[-2] = "321"
    stale_process = subprocess.run(
        argv, input=validator, text=True, capture_output=True, check=False
    )
    assert stale_process.returncode != 0
    assert "prior engine process" in stale_process.stderr

    argv[-2] = "0"
    heartbeat.write_text(
        heartbeat.read_text(encoding="utf-8").replace('"555899665"', '"other"'),
        encoding="utf-8",
    )
    wrong_account = subprocess.run(
        argv, input=validator, text=True, capture_output=True, check=False
    )
    assert wrong_account.returncode != 0
    assert "account_user_id" in wrong_account.stderr


def test_the_drill_uses_the_service_cgroup_and_requires_a_new_generation() -> None:
    text = DRILL.read_text(encoding="utf-8")
    assert 'systemctl kill --signal=KILL "$UNIT"' in text
    assert 'kill -9 "$engine_pid"' not in text
    assert 'systemctl show -p InvocationID --value "$UNIT"' in text
    assert '[ "$new_invocation_id" != "$invocation_id" ]' in text
    assert "kill_status=$?" in text
    assert "no fresh new engine generation was proved" in text


def test_a_nonzero_kill_without_generation_evidence_cannot_claim_recovery(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trace = tmp_path / "systemctl.trace"
    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SYSTEMCTL_TRACE"
case "$*" in
  "show -p MainPID --value liquidity-migration-engine.service") echo 111 ;;
  "show -p ControlGroup --value liquidity-migration-engine.service")
    echo /system.slice/liquidity-migration-engine.service ;;
  "show -p InvocationID --value liquidity-migration-engine.service")
    echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
  "kill --signal=KILL liquidity-migration-engine.service") exit 42 ;;
  *) echo "unexpected systemctl call: $*" >&2; exit 99 ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    now_ms = time.time_ns() // 1_000_000
    heartbeat = tmp_path / "heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "account_observed_wall_ts_ms": now_ms,
                "account_user_id": "555899665",
                "may_open": True,
                "mode": "live",
                "pid": 222,
                "realm": "demo",
                "venue": "bybit",
                "wall_ts_ms": now_ms,
            }
        ),
        encoding="utf-8",
    )
    proc_root = tmp_path / "proc"
    (proc_root / "222").mkdir(parents=True)
    (proc_root / "222" / "cgroup").write_text(
        "0::/system.slice/liquidity-migration-engine.service\n", encoding="utf-8"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SYSTEMCTL_TRACE": str(trace),
        "CHAOS_DRILL_PYTHON": sys.executable,
        "CHAOS_DRILL_PROC_ROOT": str(proc_root),
        "CHAOS_DRILL_WAIT_S": "0",
        "LIVENESS_ENGINE_HEARTBEAT_FILE": str(heartbeat),
        "EXPECTED_ENGINE_ACCOUNT_USER_ID": "555899665",
        "EXPECTED_ENGINE_VENUE": "bybit",
        "EXPECTED_ENGINE_REALM": "demo",
        "TELEGRAM_ENABLED": "0",
    }
    result = subprocess.run(
        ["bash", str(DRILL)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "systemctl kill returned status 42" in result.stdout
    assert "No recovery claim is possible" in result.stdout
    assert trace.read_text(encoding="utf-8").splitlines()[-1] == (
        "kill --signal=KILL liquidity-migration-engine.service"
    )


def test_a_nonzero_kill_is_accepted_only_after_proved_recovery(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    trace = tmp_path / "systemctl.trace"
    state = tmp_path / "killed"
    heartbeat = tmp_path / "heartbeat.json"
    proc_root = tmp_path / "proc"

    date = bin_dir / "date"
    date.write_text(
        """#!/usr/bin/env bash
exec "$CHAOS_DRILL_PYTHON" -c 'import time; print(time.time_ns() // 1_000_000)'
""",
        encoding="utf-8",
    )
    date.chmod(0o755)
    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep.chmod(0o755)

    systemctl = bin_dir / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "$SYSTEMCTL_TRACE"
case "$*" in
  "show -p MainPID --value liquidity-migration-engine.service")
    if [ -e "$DRILL_STATE" ]; then echo 333; else echo 111; fi ;;
  "show -p ControlGroup --value liquidity-migration-engine.service")
    echo /system.slice/liquidity-migration-engine.service ;;
  "show -p InvocationID --value liquidity-migration-engine.service")
    if [ -e "$DRILL_STATE" ]; then
      echo bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    else
      echo aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    fi ;;
  "kill --signal=KILL liquidity-migration-engine.service")
    now="$($CHAOS_DRILL_PYTHON -c 'import time; print(time.time_ns() // 1_000_000)')"
    fresh=$((now + 1000))
    mkdir -p "$CHAOS_DRILL_PROC_ROOT/444"
    printf '0::/system.slice/liquidity-migration-engine.service\n' \
      > "$CHAOS_DRILL_PROC_ROOT/444/cgroup"
    printf '{"account_observed_wall_ts_ms":%s,"account_user_id":"555899665","may_open":true,"mode":"live","pid":444,"realm":"demo","venue":"bybit","wall_ts_ms":%s}\n' \
      "$fresh" "$fresh" > "$LIVENESS_ENGINE_HEARTBEAT_FILE"
    : > "$DRILL_STATE"
    echo 'Failed to kill unit: Invalid argument' >&2
    exit 1 ;;
  "is-active --quiet liquidity-migration-engine.service")
    [ -e "$DRILL_STATE" ] ;;
  *) echo "unexpected systemctl call: $*" >&2; exit 99 ;;
esac
""",
        encoding="utf-8",
    )
    systemctl.chmod(0o755)

    now_ms = time.time_ns() // 1_000_000
    heartbeat.write_text(
        json.dumps(
            {
                "account_observed_wall_ts_ms": now_ms,
                "account_user_id": "555899665",
                "may_open": True,
                "mode": "live",
                "pid": 222,
                "realm": "demo",
                "venue": "bybit",
                "wall_ts_ms": now_ms,
            }
        ),
        encoding="utf-8",
    )
    (proc_root / "222").mkdir(parents=True)
    (proc_root / "222" / "cgroup").write_text(
        "0::/system.slice/liquidity-migration-engine.service\n", encoding="utf-8"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SYSTEMCTL_TRACE": str(trace),
        "DRILL_STATE": str(state),
        "CHAOS_DRILL_PYTHON": sys.executable,
        "CHAOS_DRILL_PROC_ROOT": str(proc_root),
        "CHAOS_DRILL_WAIT_S": "6",
        "LIVENESS_ENGINE_HEARTBEAT_FILE": str(heartbeat),
        "EXPECTED_ENGINE_ACCOUNT_USER_ID": "555899665",
        "EXPECTED_ENGINE_VENUE": "bybit",
        "EXPECTED_ENGINE_REALM": "demo",
        "TELEGRAM_ENABLED": "0",
    }
    result = subprocess.run(
        ["bash", str(DRILL)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Invalid argument" in result.stderr
    assert "systemctl returned status 1 after dispatch" in result.stdout
    assert "engine pid 222 was replaced" in result.stdout
    assert "Recovery clean" in result.stdout
    calls = trace.read_text(encoding="utf-8").splitlines()
    assert "kill --signal=KILL liquidity-migration-engine.service" in calls
    assert "is-active --quiet liquidity-migration-engine.service" in calls


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
