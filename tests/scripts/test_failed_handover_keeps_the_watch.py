"""A failed realm handover must leave the realm's liveness watchdog running.

Incident `host-51b05439c4f09794`: run `34241185290` stopped the mainnet realm's
units at 15:10:16 UTC, `start_realm` aborted at 15:10:53 on the funded engine's
unhealthy heartbeat, and `liquidity-migration-mainnet-liveness.timer` stayed
stopped while the funded engine kept running. The host watchdog paged
`CRITICAL watchdog:mainnet: mainnet watchdog timer is inactive (enabled)` from
15:11:26. The demo twin is run `34238099755` at 14:34:16 UTC.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE = ROOT / "scripts/vps/deploy_remote.sh"
MANIFEST = ROOT / "deploy/fleet_manifest.tsv"


def _function(name: str) -> str:
    source = REMOTE.read_text(encoding="utf-8")
    body = source[source.index(f"{name}() {{") :]
    return body[: body.index("\n}\n") + 3]


def _optional_function(name: str) -> str:
    # Absent before the fix: then `handover_realm` runs without it and the
    # assertions below fail on the missing restore rather than on a name.
    return _function(name) if f"{name}() {{" in REMOTE.read_text(encoding="utf-8") else ""


def _handover(realm: str, *, start_realm_fails: bool, enable_fails: bool = False) -> list[str]:
    script = f"""
set -euo pipefail
LM_FLEET_MANIFEST={MANIFEST}
LM_REALM_FIELDS={ROOT}/deploy/realm_fields.tsv
. {ROOT}/deploy/lib_sleeves.sh
PRACTICE_REALM="$(lm_practice_realm)"
fail() {{ echo "deploy failed: $*" >&2; exit 1; }}
systemctl() {{
    if [ "$1" = enable ]; then
        echo "systemctl $*"
        [ "{int(enable_fails)}" = 0 ] || return 1
    fi
}}
stop_realm_units() {{ echo "stop-realm-units $1"; }}
clear_realm_soak_overrides() {{ :; }}
retire_legacy_signal_sources() {{ :; }}
ensure_native_strategy_state() {{ :; }}
clear_reconciliation_if_requested() {{ :; }}
start_realm() {{
    echo "start-realm $1"
    [ "{int(start_realm_fails)}" = 0 ] || fail "$1 owner published an unhealthy heartbeat after startup"
}}
rollback_after_failure() {{ echo "rollback-after-failure $1"; }}
record_realm_fingerprint() {{ echo "record-fingerprint $1"; }}
{_optional_function("restore_realm_timers")}
{_function("handover_realm")}
handover_realm {realm} || echo "handover-returned-nonzero"
"""
    result = subprocess.run(["bash"], input=script, text=True, capture_output=True, check=False)
    return (result.stdout + result.stderr).splitlines()


def test_a_failed_mainnet_handover_brings_the_funded_watchdog_back() -> None:
    lines = _handover("mainnet", start_realm_fails=True)

    assert "systemctl enable --now liquidity-migration-mainnet-liveness.timer" in lines, lines
    assert "watch-restored realm=mainnet unit=liquidity-migration-mainnet-liveness.timer" in lines, lines
    assert "handover-returned-nonzero" in lines, lines


def test_the_watchdog_is_back_before_the_rollback_decision() -> None:
    lines = _handover("mainnet", start_realm_fails=True)

    restored = lines.index("watch-restored realm=mainnet unit=liquidity-migration-mainnet-liveness.timer")
    assert restored < lines.index("rollback-after-failure mainnet"), lines


def test_a_failed_demo_handover_brings_the_demo_watchdog_back() -> None:
    lines = _handover("demo", start_realm_fails=True)

    assert "systemctl enable --now liquidity-migration-demo-liveness.timer" in lines, lines


def test_every_realm_timer_the_handover_stopped_comes_back() -> None:
    expected = {
        "mainnet": [
            "liquidity-migration-mainnet-liveness.timer",
            "liquidity-migration-execution-study.timer",
        ],
        "demo": [
            "liquidity-migration-chaos-drill.timer",
            "liquidity-migration-demo-liveness.timer",
        ],
    }
    for realm, timers in expected.items():
        lines = _handover(realm, start_realm_fails=True)
        restored = {line.split("unit=")[1] for line in lines if line.startswith("watch-restored ")}
        assert restored == set(timers), (realm, restored, lines)


def test_a_restore_failure_does_not_replace_the_handover_error() -> None:
    lines = _handover("mainnet", start_realm_fails=True, enable_fails=True)

    assert any(
        line == "warning: cannot restore liquidity-migration-mainnet-liveness.timer"
        " after the failed mainnet handover"
        for line in lines
    ), lines
    assert "rollback-after-failure mainnet" in lines, lines
    assert "handover-returned-nonzero" in lines, lines


def test_a_finished_handover_restores_nothing() -> None:
    lines = _handover("mainnet", start_realm_fails=False)

    assert not [line for line in lines if line.startswith("watch-restored ")], lines
    assert "record-fingerprint mainnet" in lines, lines
