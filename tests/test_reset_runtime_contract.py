from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reset_demo_paper_ledgers.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_reset_defaults_to_dry_run_before_any_service_mutation() -> None:
    text = _text()
    assert 'MODE="dry-run"' in text
    dry_run = text.index('if [[ "$MODE" == "dry-run" ]]')
    first_stop = text.index('"$SYSTEMCTL_BIN" stop')
    assert dry_run < first_stop
    assert "DRY RUN: no services or files were changed." in text[dry_run:first_stop]


def test_reset_rejects_real_money_and_bad_routes_before_service_mutation() -> None:
    text = _text()
    first_stop = text.index('"$SYSTEMCTL_BIN" stop')
    prefix = text[:first_stop]
    assert "validate_real_money_value" in prefix
    assert "account execution roots must be pairwise disjoint" in prefix
    assert "--archive-dir must be outside reset targets" in prefix


def test_reset_stops_producers_before_both_account_owners() -> None:
    text = _text()
    units = text[text.index("STOP_UNITS=(") : text.index("ACCOUNT_BOUND_UNITS=(")]
    demo_owner = units.index("liquidity-migration-account-execution.service")
    paper_owner = units.index("liquidity-migration-account-paper-execution.service")
    for producer in (
        "liquidity-migration-bybit-long-demo.service",
        "liquidity-migration-bybit-long-paper.service",
        "liquidity-migration-bybit-continuous-demo.service",
        "liquidity-migration-bybit-continuous-paper.service",
        "liquidity-migration-continuous-hedge.service",
    ):
        assert units.index(producer) < demo_owner
        assert units.index(producer) < paper_owner


def test_reset_holds_process_and_account_leases_across_archive() -> None:
    text = _text()
    assert "LOCK_NB" in text
    assert "canonical_demo_account_lease_path" in text
    assert text.index("\nacquire_demo_account_lease\n") < text.index("tar -czf")
    assert text.index("tar -czf") < text.index('release_demo_account_lease "normal completion"')
    assert "failure recovery" in text


def test_reset_requires_explicit_flatness_including_conditional_orders() -> None:
    text = _text()
    flat = text[text.index("Checking demo/mainnet boundary") : text.index("Archiving ${#EXISTING_TARGETS")]
    assert "get_positions" in flat
    assert "get_open_orders(settle_coin=\"USDT\")" in flat
    assert 'order_filter="StopOrder"' in flat
    assert "open_positions={len(positions)} open_orders={len(orders)}" in flat


def test_reset_verifies_archive_before_deletion_and_receipt_is_last() -> None:
    text = _text()
    archive = text.index("tar -czf")
    verify = text.index("tar -tzf", archive)
    digest = text.index("archive_sha=", verify)
    deletion = text.index("rm -rf", digest)
    receipt = text.rindex("account_reset_receipt")
    assert archive < verify < digest < deletion < receipt
    assert "--leave-stopped" in text[text.index('if [[ -n "$RECEIPT_PATH" ]]') :]


def test_reset_restores_paper_ownership_and_only_shares_public_demo_inputs() -> None:
    text = _text()
    boundary = text[text.index("# Reset runs as root") : text.index("FAILURE_RECOVERY_ALLOWED=0")]
    assert "PAPER_RUNTIME_USER" in boundary
    assert 'chown -R "$PAPER_RUNTIME_USER:$PAPER_RUNTIME_GROUP"' in boundary
    assert "store.parquet" in boundary
    assert "residual_momentum.parquet" in boundary
    assert 'chown -R "root:$PAPER_RUNTIME_GROUP"' not in boundary
    assert 'find "$root" -type f -exec chmod 0640' not in boundary
