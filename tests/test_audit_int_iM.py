"""Audit integration bucket iM — cross-file completion regression tests.

deploy-env-timers-3: the continuous-PAPER systemd unit hard-coded
``Environment=KLINES_FOLLOW_ROOT=data/bybit-continuous-demo-event`` so the paper
shadow followed the demo (leader) sleeve's flushed kline snapshot read-only to
halve WS decode CPU. That follow is only safe while the demo sleeve is ON. The
documented-valid ``CONTINUOUS_SLEEVE=off`` + ``CONTINUOUS_PAPER_SLEEVE=on`` combo
(``continuous_rmom_refresh_on()`` is true if EITHER sleeve is on, pinned by
tests/test_sleeve_kill_switch.py) stops the demo daemon and freezes its kline
store, leaving the shadow following a stale snapshot for up to ~2 days while it
appears healthy — which defeats the paper sleeve's whole purpose (forward
execution/cost calibration that feeds promotion context).

The robust completion (per the audit's SAFE OPTION) drops the follow override
from the PAPER unit so the shadow always streams its own kline pool and stays
live regardless of the demo sleeve's toggle state. These tests pin that the
override is gone, that no reference to the demo root survives in the paper unit's
environment, and that the rest of the (load-bearing) paper config is undisturbed.
"""

from __future__ import annotations

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_PAPER_UNIT = (
    _REPO
    / "deploy"
    / "systemd"
    / "liquidity-migration-bybit-continuous-paper.service"
)


def _unit_text() -> str:
    return _PAPER_UNIT.read_text(encoding="utf-8")


def _environment_assignments() -> dict[str, str]:
    """Parse the unit's active ``Environment=KEY=VALUE`` lines (skip comments)."""
    env: dict[str, str] = {}
    for raw in _unit_text().splitlines():
        line = raw.strip()
        if line.startswith("#") or not line.startswith("Environment="):
            continue
        assignment = line[len("Environment=") :]
        key, _, value = assignment.partition("=")
        env[key] = value
    return env


def test_paper_unit_no_longer_follows_demo_kline_root() -> None:
    """deploy-env-timers-3: the PAPER shadow must not carry a KLINES_FOLLOW_ROOT
    override, so it always runs its own kline pool and never follows a frozen demo
    snapshot when CONTINUOUS_SLEEVE=off + CONTINUOUS_PAPER_SLEEVE=on."""
    env = _environment_assignments()
    assert "KLINES_FOLLOW_ROOT" not in env, (
        "PAPER unit still sets KLINES_FOLLOW_ROOT — it would follow the demo "
        "kline store and freeze when the demo sleeve is toggled off"
    )


def test_paper_unit_environment_never_points_at_demo_root() -> None:
    """No active Environment= assignment in the PAPER unit may reference the demo
    data root: the shadow's market-data plane must be self-contained."""
    env = _environment_assignments()
    offenders = {
        key: value
        for key, value in env.items()
        if "bybit-continuous-demo-event" in value
    }
    assert not offenders, (
        f"PAPER unit Environment assignments still point at the demo root: {offenders}"
    )


def test_paper_unit_keeps_its_own_paper_data_root() -> None:
    """The paper sleeve must still write/read its own dataset root so reconcile can
    pair it against the demo ledger — only the follow override was removed."""
    env = _environment_assignments()
    assert env.get("DATA_ROOT") == "data/bybit-continuous-paper-event", (
        "PAPER unit lost or changed its own DATA_ROOT"
    )


def test_paper_unit_load_bearing_paper_knobs_intact() -> None:
    """Dropping the follow line must not disturb the knobs that make this a true
    no-submit shadow of the demo book (PAPER_MODE/dry-run routing + the mirrored
    strategy knobs)."""
    env = _environment_assignments()
    for key, expected in (
        ("SUBMIT_ORDERS", "0"),
        ("RECORD_DRY_RUN", "1"),
        ("PAPER_MODE", "1"),
        ("STRATEGY_PROFILE", "continuous_ensemble_v1"),
    ):
        assert env.get(key) == expected, (
            f"PAPER unit knob {key} changed: expected {expected!r}, got {env.get(key)!r}"
        )


def test_paper_unit_documents_the_dropped_follow_override() -> None:
    """The removal is documented in-unit (audit id + rationale) so an operator
    re-adding the follow knob understands the demo-off hazard."""
    text = _unit_text()
    assert "deploy-env-timers-3" in text, (
        "the dropped KLINES_FOLLOW_ROOT override should be documented with its audit id"
    )
