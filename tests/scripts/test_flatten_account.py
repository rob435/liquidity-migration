from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "vps" / "flatten_account.sh"


def _heartbeat(*, symbols: tuple[str, ...] = (), **overrides: object) -> dict:
    now_ms = int(time.time() * 1_000)
    beat: dict[str, object] = {
        "account_observed_wall_ts_ms": now_ms,
        "account_user_id": "555899665",
        "engine_version": "engine-core 0.1.0",
        "positions": [
            {"symbol": symbol, "qty": 1.0, "side": "long", "entry_px": 1.0}
            for symbol in symbols
        ],
        "realm": "demo",
        "venue": "bybit",
        "wall_ts_ms": now_ms,
    }
    beat.update(overrides)
    return beat


def _write_fake_tools(tmp_path: Path) -> Path:
    tools = tmp_path / "bin"
    tools.mkdir()
    systemctl = tools / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$FLATTEN_TEST_SYSTEMCTL_LOG"
case "$1" in
  is-active)
    count_file="$FLATTEN_TEST_IS_ACTIVE_COUNT"
    count=0
    [ ! -f "$count_file" ] || count="$(cat "$count_file")"
    count=$((count + 1))
    printf '%s' "$count" > "$count_file"
    [ "$count" -lt "${FLATTEN_TEST_ENGINE_FAIL_AT:-999}" ]
    ;;
  stop)
    [ "${FLATTEN_TEST_STOP_FAIL:-}" != "$2" ]
    ;;
  show)
    printf '%s\n' "${FLATTEN_TEST_ACTIVE_STATE:-inactive}"
    ;;
  *) exit 2 ;;
esac
"""
    )
    systemctl.chmod(0o755)

    sleep = tools / "sleep"
    sleep.write_text(
        """#!/usr/bin/env bash
set -eu
python3 - "$1" <<'PY'
import sys
import time
time.sleep(float(sys.argv[1]))
PY
if [ -n "${FLATTEN_TEST_NEXT_HEARTBEAT:-}" ]; then
  cp "$FLATTEN_TEST_NEXT_HEARTBEAT" "$FLATTEN_HEARTBEAT_PATH"
fi
python3 - "$FLATTEN_HEARTBEAT_PATH" <<'PY'
import json
import sys
import time
path = sys.argv[1]
try:
    with open(path) as handle:
        beat = json.load(handle)
except Exception:
    raise SystemExit(0)
now_ms = int(time.time() * 1_000)
beat["wall_ts_ms"] = now_ms
beat["account_observed_wall_ts_ms"] = now_ms
with open(path, "w") as handle:
    json.dump(beat, handle)
PY
"""
    )
    sleep.chmod(0o755)
    return tools


def _run(
    tmp_path: Path,
    heartbeat: dict | str | None,
    *,
    execute: bool = True,
    extra_env: dict[str, str] | None = None,
    next_heartbeat: dict | str | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    heartbeat_path = tmp_path / "heartbeat.json"
    if isinstance(heartbeat, dict):
        heartbeat_path.write_text(json.dumps(heartbeat))
    elif isinstance(heartbeat, str):
        heartbeat_path.write_text(heartbeat)

    engine_env = tmp_path / "engine.env"
    engine_env.write_text(
        "\n".join(
            (
                "EXPECTED_ENGINE_ACCOUNT_USER_ID=555899665",
                "EXPECTED_ENGINE_VENUE=bybit",
                "EXPECTED_ENGINE_REALM=demo",
                "EXPECTED_ENGINE_VERSION=engine-core 0.1.0",
                "",
            )
        )
    )
    target_root = tmp_path / "targets"
    systemctl_log = tmp_path / "systemctl.log"
    tools = _write_fake_tools(tmp_path)
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tools}{os.pathsep}{environment['PATH']}",
            "FLATTEN_ENGINE_ENV_PATH": str(engine_env),
            "FLATTEN_HEARTBEAT_PATH": str(heartbeat_path),
            "FLATTEN_MAX_HEARTBEAT_AGE_SECONDS": "30",
            "FLATTEN_POLL_SECONDS": "0.01",
            "FLATTEN_TARGET_ROOT": str(target_root),
            "FLATTEN_TEST_IS_ACTIVE_COUNT": str(tmp_path / "is-active-count"),
            "FLATTEN_TEST_SYSTEMCTL_LOG": str(systemctl_log),
        }
    )
    if next_heartbeat is not None:
        next_path = tmp_path / "next-heartbeat.json"
        if isinstance(next_heartbeat, dict):
            next_path.write_text(json.dumps(next_heartbeat))
        else:
            next_path.write_text(next_heartbeat)
        environment["FLATTEN_TEST_NEXT_HEARTBEAT"] = str(next_path)
    if extra_env:
        environment.update(extra_env)

    command = ["bash", str(SCRIPT), "--environment", "demo", "--wait-seconds", "2"]
    command.append("--execute" if execute else "--dry-run")
    result = subprocess.run(command, env=environment, text=True, capture_output=True, check=False)
    return result, target_root, systemctl_log


def test_execute_stops_producers_even_when_already_flat(tmp_path: Path) -> None:
    result, target_root, systemctl_log = _run(tmp_path, _heartbeat())

    assert result.returncode == 6, result.stderr
    calls = systemctl_log.read_text().splitlines()
    for unit in (
        "liquidity-migration-bybit-carry-demo.service",
        "liquidity-migration-bybit-long-demo.service",
    ):
        assert f"stop {unit}" in calls
        assert f"show --property=ActiveState --value {unit}" in calls
    assert "configured_positions_closed global_flat=unproven" in result.stderr
    assert "waiting for post-write heartbeat" in result.stdout
    for name in ("carry-demo.json", "long-demo.json", "exodus-demo.json"):
        assert json.loads((target_root / name).read_text())["targets"] == []


def test_a_stop_failure_aborts_before_any_book_write(tmp_path: Path) -> None:
    failed = "liquidity-migration-bybit-carry-demo.service"
    result, target_root, _ = _run(
        tmp_path,
        _heartbeat(symbols=("BTCUSDT",)),
        extra_env={"FLATTEN_TEST_STOP_FAIL": failed},
    )

    assert result.returncode == 5
    assert f"failed to stop producer unit={failed}; no books written" in result.stderr
    assert not target_root.exists()


def test_missing_inactive_proof_aborts_before_any_book_write(tmp_path: Path) -> None:
    result, target_root, _ = _run(
        tmp_path,
        _heartbeat(symbols=("BTCUSDT",)),
        extra_env={"FLATTEN_TEST_ACTIVE_STATE": "active"},
    )

    assert result.returncode == 5
    assert "state=active, expected inactive; no books written" in result.stderr
    assert not target_root.exists()


@pytest.mark.parametrize(
    "condition",
    [
        "missing",
        "malformed",
        "stale-heartbeat",
        "stale-account-view",
        "wrong-account",
        "wrong-venue",
        "wrong-realm",
        "wrong-version",
        "positions-unknown",
    ],
)
def test_unknown_heartbeat_never_means_flat(
    tmp_path: Path, condition: str
) -> None:
    heartbeat: dict | str | None
    if condition == "missing":
        heartbeat = None
    elif condition == "malformed":
        heartbeat = "{not json\n"
    else:
        changes: dict[str, object] = {
            "stale-heartbeat": {"wall_ts_ms": 1},
            "stale-account-view": {"account_observed_wall_ts_ms": 1},
            "wrong-account": {"account_user_id": "wrong-account"},
            "wrong-venue": {"venue": "wrong-venue"},
            "wrong-realm": {"realm": "mainnet"},
            "wrong-version": {"engine_version": "wrong-version"},
            "positions-unknown": {"positions": None},
        }[condition]
        heartbeat = _heartbeat(**changes)
    result, target_root, systemctl_log = _run(tmp_path, heartbeat)

    assert result.returncode == 4
    assert "configured-position state is unknown" in result.stderr
    assert "configured_positions_closed" not in result.stderr
    assert not target_root.exists()
    calls = systemctl_log.read_text().splitlines()
    assert sum(line.startswith("stop ") for line in calls) == 2
    assert sum(line.startswith("show ") for line in calls) == 2


def test_a_dry_run_with_unknown_heartbeat_changes_nothing(tmp_path: Path) -> None:
    result, target_root, systemctl_log = _run(
        tmp_path,
        "{not json\n",
        execute=False,
    )

    assert result.returncode == 4
    assert "configured-position state is unknown" in result.stderr
    assert not target_root.exists()
    assert not any(line.startswith("stop ") for line in systemctl_log.read_text().splitlines())


def test_a_fresh_flat_heartbeat_after_writes_closes_configured_positions(
    tmp_path: Path,
) -> None:
    result, target_root, _ = _run(
        tmp_path,
        _heartbeat(symbols=("BTCUSDT",)),
        next_heartbeat=_heartbeat(),
    )

    assert result.returncode == 6, result.stderr
    assert "configured_positions_closed global_flat=unproven" in result.stderr
    for name in ("carry-demo.json", "long-demo.json", "exodus-demo.json"):
        rows = json.loads((target_root / name).read_text())["targets"]
        assert [row["symbol"] for row in rows] == ["BTCUSDT"]


def test_a_broken_heartbeat_after_writes_cannot_report_closed(tmp_path: Path) -> None:
    result, target_root, _ = _run(
        tmp_path,
        _heartbeat(symbols=("BTCUSDT",)),
        next_heartbeat="{broken\n",
    )

    assert result.returncode == 5
    assert "status=heartbeat_unknown global_flat=unproven" in result.stderr
    assert "configured_positions_closed" not in result.stderr
    assert target_root.exists(), "zero books were already written before telemetry broke"
