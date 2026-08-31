from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "vps" / "flatten_account.sh"
DIRECTIONAL = ("long", "carry", "exodus")


def _heartbeat(
    *,
    symbols: tuple[str, ...] = (),
    entries_enabled: bool = True,
    pending: tuple[tuple[str, str], ...] = (),
    working: tuple[tuple[str, str], ...] = (),
    **overrides: object,
) -> dict[str, object]:
    now_ms = int(time.time() * 1_000)
    beat: dict[str, object] = {
        "account_observed_wall_ts_ms": now_ms,
        "account_user_id": "555899665",
        "pending_flatten_requests": [
            {"strategy": strategy, "request_id": request_id}
            for strategy, request_id in pending
        ],
        "positions": [
            {
                "symbol": symbol,
                "qty": 1.0,
                "side": "long",
                "entry_px": 1.0,
                "strategy": "long",
            }
            for symbol in symbols
        ],
        "realm": "demo",
        "strategy_entries_enabled": [
            {"strategy": strategy, "entries_enabled": entries_enabled}
            for strategy in DIRECTIONAL
        ],
        "venue": "bybit",
        "wall_ts_ms": now_ms,
        "working_entries": [
            {"strategy": strategy, "symbol": symbol}
            for strategy, symbol in working
        ],
    }
    beat.update(overrides)
    return beat


def _write_fake_tools(
    tmp_path: Path, heartbeat_path: Path, next_path: Path | None
) -> tuple[Path, Path, Path]:
    tools = tmp_path / "bin"
    tools.mkdir()
    engine_log = tmp_path / "engine.log"

    systemctl = tools / "systemctl"
    systemctl.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >> "$FLATTEN_TEST_SYSTEMCTL_LOG"
[ "$1" = is-active ]
"""
    )
    systemctl.chmod(0o755)

    sleep = tools / "sleep"
    copy = (
        f"cp {shlex.quote(str(next_path))} {shlex.quote(str(heartbeat_path))}\n"
        if next_path is not None
        else ""
    )
    sleep.write_text(
        "#!/usr/bin/env bash\nset -eu\n"
        + copy
        + f"python3 - {shlex.quote(str(heartbeat_path))} <<'PY'\n"
        + "import json, sys, time\n"
        + "path = sys.argv[1]\n"
        + "with open(path, encoding='utf-8') as handle: beat = json.load(handle)\n"
        + "now = int(time.time() * 1000)\n"
        + "beat['wall_ts_ms'] = now\nbeat['account_observed_wall_ts_ms'] = now\n"
        + "with open(path, 'w', encoding='utf-8') as handle: json.dump(beat, handle)\n"
        + "PY\n"
    )
    sleep.chmod(0o755)

    engine = tmp_path / "engine"
    engine.write_text(
        "#!/usr/bin/env bash\nset -eu\n"
        + f"printf '%s\\n' \"$*\" >> {shlex.quote(str(engine_log))}\n"
        + "command=$1\nshift\nstrategy=\nrequest_id=\nenabled=\n"
        + "while [ \"$#\" -gt 0 ]; do\n"
        + "  case \"$1\" in\n"
        + "    --strategy) strategy=$2; shift 2 ;;\n"
        + "    --request-id) request_id=$2; shift 2 ;;\n"
        + "    --entries-enabled) enabled=$2; shift 2 ;;\n"
        + "    *) shift ;;\n"
        + "  esac\n"
        + "done\n"
        + f"python3 - {shlex.quote(str(heartbeat_path))} \"$command\" \"$strategy\" \"$request_id\" \"$enabled\" <<'PY'\n"
        + "import json, sys, time\n"
        + "path, command, strategy, request_id, enabled = sys.argv[1:]\n"
        + "with open(path, encoding='utf-8') as handle: beat = json.load(handle)\n"
        + "if command == 'set-strategy-entry-permission':\n"
        + "    for row in beat['strategy_entries_enabled']:\n"
        + "        if row['strategy'] == strategy: row['entries_enabled'] = enabled == 'true'\n"
        + "elif command == 'flatten-strategy':\n"
        + "    beat['pending_flatten_requests'].append({'strategy': strategy, 'request_id': request_id})\n"
        + "now = int(time.time() * 1000)\n"
        + "beat['wall_ts_ms'] = now\nbeat['account_observed_wall_ts_ms'] = now\n"
        + "with open(path, 'w', encoding='utf-8') as handle: json.dump(beat, handle)\n"
        + "PY\n"
    )
    engine.chmod(0o755)

    setpriv = tmp_path / "setpriv"
    setpriv.write_text(
        """#!/usr/bin/env bash
set -eu
while [ "$#" -gt 0 ] && [ "$1" != /usr/bin/env ]; do shift; done
[ "$#" -gt 0 ] || exit 2
exec "$@"
"""
    )
    setpriv.chmod(0o755)
    return tools, engine, setpriv


def _run(
    tmp_path: Path,
    heartbeat: dict[str, object] | str | None,
    *,
    execute: bool = True,
    next_heartbeat: dict[str, object] | str | None = None,
    wait_seconds: int = 1,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    heartbeat_path = tmp_path / "heartbeat.json"
    if isinstance(heartbeat, dict):
        heartbeat_path.write_text(json.dumps(heartbeat))
    elif isinstance(heartbeat, str):
        heartbeat_path.write_text(heartbeat)

    next_path = None
    if next_heartbeat is not None:
        next_path = tmp_path / "next-heartbeat.json"
        next_path.write_text(
            json.dumps(next_heartbeat)
            if isinstance(next_heartbeat, dict)
            else next_heartbeat
        )

    engine_env = tmp_path / "engine.env"
    engine_env.write_text(
        "EXPECTED_ENGINE_ACCOUNT_USER_ID=555899665\n"
        "EXPECTED_ENGINE_VENUE=bybit\n"
        "EXPECTED_ENGINE_REALM=demo\n"
    )
    engine_config = tmp_path / "engine.toml"
    engine_config.write_text("[engine]\n")
    tools, engine, setpriv = _write_fake_tools(tmp_path, heartbeat_path, next_path)

    runnable = tmp_path / "flatten-account-test.sh"
    runnable.write_text(SCRIPT.read_text().replace("/usr/bin/setpriv", str(setpriv)))
    runnable.chmod(0o755)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{tools}{os.pathsep}{environment['PATH']}",
            "FLATTEN_ENGINE_BINARY": str(engine),
            "FLATTEN_ENGINE_CONFIG_PATH": str(engine_config),
            "FLATTEN_ENGINE_ENV_PATH": str(engine_env),
            "FLATTEN_HEARTBEAT_PATH": str(heartbeat_path),
            "FLATTEN_MAX_HEARTBEAT_AGE_SECONDS": "30",
            "FLATTEN_POLL_SECONDS": "0.01",
            "FLATTEN_TEST_SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"),
        }
    )
    command = [
        "bash",
        str(runnable),
        "--environment",
        "demo",
        "--wait-seconds",
        str(wait_seconds),
        "--execute" if execute else "--dry-run",
    ]
    result = subprocess.run(command, env=environment, text=True, capture_output=True, check=False)
    systemctl_calls = (tmp_path / "systemctl.log").read_text().splitlines()
    engine_calls = (
        (tmp_path / "engine.log").read_text().splitlines()
        if (tmp_path / "engine.log").exists()
        else []
    )
    return result, systemctl_calls, engine_calls


def test_dry_run_only_reads_the_engine_and_never_stops_signal_ingestion(tmp_path: Path) -> None:
    result, systemctl_calls, engine_calls = _run(
        tmp_path, _heartbeat(symbols=("BTCUSDT",)), execute=False
    )

    assert result.returncode == 0, result.stderr
    assert systemctl_calls == ["is-active --quiet liquidity-migration-engine.service"]
    assert engine_calls == []
    assert "would set entries_enabled=false strategy=long" in result.stdout
    assert "would request flatten strategy=exodus" in result.stdout
    assert "signal-worker" not in result.stdout + result.stderr


def test_execute_waits_for_flat_reducer_acks_and_no_directional_open_orders(tmp_path: Path) -> None:
    result, systemctl_calls, engine_calls = _run(
        tmp_path,
        _heartbeat(symbols=("BTCUSDT",)),
        next_heartbeat=_heartbeat(entries_enabled=False),
        wait_seconds=2,
    )

    assert result.returncode == 6, result.stderr
    assert systemctl_calls == ["is-active --quiet liquidity-migration-engine.service"]
    assert len(engine_calls) == 6
    assert sum(call.startswith("set-strategy-entry-permission ") for call in engine_calls) == 3
    assert sum(call.startswith("flatten-strategy ") for call in engine_calls) == 3
    assert "status=engine_positions_closed" in result.stderr
    assert "entries remain paused" in result.stderr
    assert all("signal-worker" not in call for call in systemctl_calls + engine_calls)


def test_execute_does_not_treat_spool_acceptance_as_reducer_ack(tmp_path: Path) -> None:
    result, _, _ = _run(tmp_path, _heartbeat(), wait_seconds=0)
    assert result.returncode == 5
    assert "pending_flatten_acks=3" in result.stdout
    assert "status=engine_positions_closed" not in result.stderr


@pytest.mark.parametrize(
    ("symbols", "working", "expected_progress"),
    [
        (
            ("BTCUSDT",),
            (),
            "still held=BTCUSDT",
        ),
        (
            (),
            (("carry", "ETHUSDT"),),
            "directional_working_entries=1",
        ),
    ],
)
def test_execute_refuses_to_finish_while_any_completion_fact_is_open(
    tmp_path: Path,
    symbols: tuple[str, ...],
    working: tuple[tuple[str, str], ...],
    expected_progress: str,
) -> None:
    heartbeat = _heartbeat(
        symbols=symbols,
        entries_enabled=False,
        working=working,
    )
    result, _, _ = _run(tmp_path, heartbeat, wait_seconds=0)
    assert result.returncode == 5
    assert "status=timed_out" in result.stderr
    assert expected_progress in result.stdout


@pytest.mark.parametrize(
    "heartbeat",
    [
        None,
        "{broken\n",
        _heartbeat(wall_ts_ms=1),
        _heartbeat(account_observed_wall_ts_ms=1),
        _heartbeat(account_user_id="wrong"),
        _heartbeat(venue="wrong"),
        _heartbeat(realm="mainnet"),
        _heartbeat(positions=None),
        _heartbeat(strategy_entries_enabled=None),
        _heartbeat(pending_flatten_requests=None),
        _heartbeat(working_entries=None),
    ],
)
def test_unknown_heartbeat_never_means_flat(
    tmp_path: Path, heartbeat: dict[str, object] | str | None
) -> None:
    result, systemctl_calls, engine_calls = _run(tmp_path, heartbeat)
    assert result.returncode == 4
    assert "engine state is unknown" in result.stderr
    assert systemctl_calls == ["is-active --quiet liquidity-migration-engine.service"]
    assert engine_calls == []


def test_flat_dry_run_is_evidence_only_and_changes_nothing(tmp_path: Path) -> None:
    result, _, engine_calls = _run(tmp_path, _heartbeat(), execute=False)
    assert result.returncode == 6
    assert "global_flat=unproven" in result.stdout
    assert engine_calls == []
