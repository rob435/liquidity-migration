"""Cross-file integration-completion regression tests for audit bucket iI.

Each test exercises the foreign-file half of a deploy-gate fix whose owned-file
side (scripts/deploy_vps_live.sh) landed in another bucket. The two scripts here
are SSH/systemctl deploy plumbing that cannot run in CI, so — matching the
existing deploy-script regression style in tests/test_runtime_scripts.py and
tests/test_audit_int_iF.py — these tests assert the static content of the
fail-closed guards. Written to FAIL on the pre-completion code and PASS now.

Findings covered:

  deploy-ci-6  verify_vps_live.sh and vps_console_recover_and_deploy.sh source
               bybit-demo.env and assert TELEGRAM_CHAT_ID but never asserted the
               highest-stakes REAL_MONEY toggle. Both now carry the same
               fail-closed `case "${REAL_MONEY:-}" in 1|true|...) exit 1` guard
               as deploy_vps_live.sh, so a mis-edited env that sets REAL_MONEY
               truthy is caught by the verify/recovery paths too — not only by
               the per-process runtime guard validate_order_submit_allowed().

  deploy-ci-3  The console-recovery path enables+restarts the always-on
               liquidation collector but its post-settle verify block never
               asserted the collector is active+enabled, so a recovered code
               change that crashes the collector still reached
               'deploy-verify-ok'. The verify block now checks
               `is-active`/`is-enabled` for the collector, matching the fix in
               deploy_vps_live.sh.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VERIFY = REPO / "scripts" / "verify_vps_live.sh"
RECOVERY = REPO / "scripts" / "vps_console_recover_and_deploy.sh"

COLLECTOR = "liquidity-migration-liquidation-collector.service"
# The truthy-REAL_MONEY case arm shared verbatim with deploy_vps_live.sh.
REAL_MONEY_CASE_ARM = "1|true|TRUE|True|yes|YES|Yes|on|ON|On)"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# deploy-ci-6 : fail-closed REAL_MONEY guard in BOTH owned scripts
# --------------------------------------------------------------------------


def _assert_real_money_guard(text: str, *, refusal_token: str) -> None:
    """A `case "${REAL_MONEY:-}"` guard whose truthy arm exits non-zero."""
    assert 'case "${REAL_MONEY:-}" in' in text, "missing REAL_MONEY case guard"
    assert REAL_MONEY_CASE_ARM in text, "REAL_MONEY guard does not match the truthy arm set"
    # The guard must be fail-closed: the truthy arm exits 1, and it references
    # the env file in the refusal so an operator knows what to fix.
    guard = text.split('case "${REAL_MONEY:-}" in', 1)[1].split("esac", 1)[0]
    assert REAL_MONEY_CASE_ARM in guard
    assert "exit 1" in guard, "REAL_MONEY guard must exit 1 on a truthy value"
    assert "REAL_MONEY" in guard
    assert refusal_token in guard
    assert "/etc/liquidity-migration/bybit-demo.env" in guard


def test_verify_script_fails_closed_on_real_money() -> None:
    text = _read(VERIFY)
    _assert_real_money_guard(text, refusal_token="Verification failed")
    # The guard must come AFTER the env is sourced, else ${REAL_MONEY} is unset.
    source_idx = text.index(". /etc/liquidity-migration/bybit-demo.env")
    guard_idx = text.index('case "${REAL_MONEY:-}" in')
    assert source_idx < guard_idx, "REAL_MONEY guard must be after sourcing the env"


def test_recovery_script_fails_closed_on_real_money() -> None:
    text = _read(RECOVERY)
    _assert_real_money_guard(text, refusal_token="Refusing deploy")
    source_idx = text.index(". /etc/liquidity-migration/bybit-demo.env")
    guard_idx = text.index('case "${REAL_MONEY:-}" in')
    assert source_idx < guard_idx, "REAL_MONEY guard must be after sourcing the env"


def test_real_money_guard_does_not_accept_demo_or_unset() -> None:
    """The fail-closed arm must only match truthy spellings — demo / false /
    unset must NOT trip it (that would block every legitimate demo deploy)."""
    truthy = {"1", "true", "TRUE", "True", "yes", "YES", "Yes", "on", "ON", "On"}
    benign = {"", "0", "false", "FALSE", "False", "no", "NO", "demo", "DEMO"}
    arm = REAL_MONEY_CASE_ARM.rstrip(")")
    patterns = arm.split("|")
    for value in truthy:
        assert value in patterns, f"truthy {value!r} must trip the guard"
    for value in benign:
        assert value not in patterns, f"benign {value!r} must NOT trip the guard"


# --------------------------------------------------------------------------
# deploy-ci-3 : recovery verify block asserts the liquidation collector is up
# --------------------------------------------------------------------------


def test_recovery_enables_and_restarts_the_collector() -> None:
    """Sanity precondition for the finding: the recovery path DOES bring the
    always-on collector up, so the verify block owes it an is-active check."""
    text = _read(RECOVERY)
    assert f"systemctl enable {COLLECTOR}" in text
    assert f"systemctl restart {COLLECTOR}" in text


def test_recovery_verify_block_checks_collector_active_and_enabled() -> None:
    text = _read(RECOVERY)
    assert f"systemctl is-active --quiet {COLLECTOR}" in text, (
        "recovery verify must assert the liquidation collector is active "
        "(catches a crash-loop reaching 'failed')"
    )
    assert f"systemctl is-enabled --quiet {COLLECTOR}" in text, (
        "recovery verify must assert the liquidation collector is enabled"
    )


def test_recovery_collector_verify_is_in_the_post_settle_block_before_verify_ok() -> None:
    """The collector check must sit in the POST-settle verify block (after the
    sleep) and BEFORE 'deploy-verify-ok' is emitted — otherwise a broken
    collector still reaches the success message + Telegram."""
    text = _read(RECOVERY)
    # Post-settle block begins at the settle sleep guard.
    settle_idx = text.index('if [ "$SYSTEMD_SETTLE_SECONDS" -gt 0 ]; then')
    # Anchor on the actual success echo, NOT any mention of the string (the
    # deploy-ci-3 comment block also references 'deploy-verify-ok').
    verify_ok_idx = text.index('echo "deploy-verify-ok')
    is_active_idx = text.index(f"systemctl is-active --quiet {COLLECTOR}")
    is_enabled_idx = text.index(f"systemctl is-enabled --quiet {COLLECTOR}")
    assert settle_idx < is_active_idx < verify_ok_idx
    assert settle_idx < is_enabled_idx < verify_ok_idx


def test_recovery_collector_verify_matches_risk_service_pattern() -> None:
    """Parity check: the collector is verified the SAME way as the risk service
    (both is-active and is-enabled, --quiet), so the gate fails loud."""
    text = _read(RECOVERY)
    risk = "liquidity-migration-bybit-risk.service"
    for unit in (risk, COLLECTOR):
        assert re.search(
            rf"^\s*systemctl is-active --quiet {re.escape(unit)}\s*$", text, re.MULTILINE
        ), f"missing is-active --quiet for {unit}"
        assert re.search(
            rf"^\s*systemctl is-enabled --quiet {re.escape(unit)}\s*$", text, re.MULTILINE
        ), f"missing is-enabled --quiet for {unit}"
