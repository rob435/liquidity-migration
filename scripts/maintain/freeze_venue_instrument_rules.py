#!/usr/bin/env python3
"""Freeze instrument rules from Bybit's read-only instruments-info endpoint.

The off-demo counterpart to ``probe_bybit_demo_rules.py``, which finds demo's
empirical minimum notional by submitting and cancelling real PostOnly orders —
a technique that costs money on a funded account. This reads the venue's
declared rules instead and places no orders.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import hashlib

from liquidity_migration.venue.bybit import (
    BybitAccountReader,
    resolve_private_credentials,
)
from liquidity_migration.core.logging_setup import ensure_default_log_handler
from liquidity_migration.strategy.account_candidate_universe import (
    account_exposure_labels,
    load_candidate_universe,
)
from liquidity_migration.account.account_route import require_account_route
from liquidity_migration.policy.execution_environment import (
    EXECUTION_ENVIRONMENT_VALUES,
    account_id_for_environment,
)
from liquidity_migration.core.artifact_snapshot import read_stable_file
from liquidity_migration.venue.venue_instrument_rules import (
    build_venue_instrument_rules,
    candidate_symbol_source,
    load_venue_rules_bytes,
    render_venue_rules_artifact,
)
from liquidity_migration.core.venue_realm import (
    REALM_CREDENTIAL_VARIABLES,
    VenueRealm,
    venue_realm,
)


def _symbols(path: Path) -> list[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("symbols") or list(payload.get("rules") or {})
    else:
        raise ValueError("symbols file must be a list or an object with 'symbols'")
    symbols = sorted({str(row).strip().upper() for row in rows if str(row).strip()})
    if not symbols:
        raise ValueError(f"no symbols in {path}")
    return symbols


def main(argv: list[str] | None = None) -> int:
    ensure_default_log_handler()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--realm",
        required=True,
        choices=tuple(realm.value for realm in VenueRealm),
        help="Venue realm to read rules from. Required; there is no default.",
    )
    parser.add_argument(
        "--symbols-file",
        required=True,
        help=(
            "Frozen candidate-universe artifact. The receipt is bound to its exact "
            "bytes, so authorization can prove the rules cover this universe."
        ),
    )
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--unbound-symbol-list",
        action="store_true",
        help=(
            "Accept a plain symbol list instead of a candidate-universe artifact. "
            "The receipt then carries no source binding and cannot satisfy the "
            "operational-authority coverage proof; diagnostics only."
        ),
    )
    parser.add_argument(
        "--held-exposure-account-root",
        default="",
        help=(
            "Account journal root to scan for symbols the account still has "
            "exposure on. Those symbols get rules beyond the universe, so a "
            "held position whose symbol left the entry population can still "
            "be exited. Requires --held-exposure-inbox-root."
        ),
    )
    parser.add_argument(
        "--held-exposure-inbox-root",
        default="",
        help="Intent inbox root paired with --held-exposure-account-root.",
    )
    parser.add_argument(
        "--prior-rules-file",
        default="",
        help=(
            "Previous rules receipt. An exposure symbol the venue no longer "
            "lists carries its structural rule forward from here, so its "
            "remaining exits can still be built and can die properly on the "
            "venue's definite reject."
        ),
    )
    args = parser.parse_args(argv)

    realm = venue_realm(args.realm)
    output = Path(args.output).expanduser()
    if output.exists():
        parser.error(f"refusing to overwrite an existing rules artifact: {output}")

    symbols_path = Path(args.symbols_file).expanduser()
    symbol_source = None
    if not args.unbound_symbol_list:
        try:
            snapshot = read_stable_file(
                symbols_path,
                label="candidate-universe artifact",
                require_single_link=False,
            )
            # The universe artifact records the realm it was frozen from and its
            # loader refuses any other, so ``realm`` must be passed through.
            candidate = load_candidate_universe(
                symbols_path, snapshot=snapshot, realm=realm
            )
        except Exception as exc:  # noqa: BLE001 - reported to the operator verbatim
            parser.error(
                f"{symbols_path} is not a readable candidate-universe artifact ({exc}). "
                "Freeze the universe first, or pass --unbound-symbol-list for a "
                "diagnostics-only receipt."
            )
        symbol_source = candidate_symbol_source(candidate, size_bytes=snapshot.size)

    api_key, api_secret = resolve_private_credentials(realm=realm)
    if not api_key or not api_secret:
        key_variable, secret_variable = REALM_CREDENTIAL_VARIABLES[realm]
        parser.error(f"{key_variable} and {secret_variable} are required")

    client = BybitAccountReader(
        category="linear",
        testnet=False,
        demo=realm is VenueRealm.DEMO,
        realm=realm,
        api_key=api_key,
        api_secret=api_secret,
        # No mutation lease on purpose: every call this script makes is a read.
    )
    universe_symbols = set(_symbols(symbols_path))
    exposure_symbols: set[str] = set()
    if bool(args.held_exposure_account_root) != bool(args.held_exposure_inbox_root):
        parser.error(
            "--held-exposure-account-root and --held-exposure-inbox-root "
            "must be passed together"
        )
    if args.held_exposure_account_root:
        if realm.value not in EXECUTION_ENVIRONMENT_VALUES:
            parser.error(f"held-exposure scan has no account owner for realm {realm.value}")
        account_root = Path(args.held_exposure_account_root).expanduser()
        inbox_root = Path(args.held_exposure_inbox_root).expanduser()
        if account_root.exists():
            # Any failure here is a hard stop: minting a receipt that silently
            # omits held symbols is exactly the wedge this scan prevents.
            route = require_account_route(
                account_id=account_id_for_environment(realm.value),
                environment=realm.value,
                account_root=account_root,
                inbox_root=inbox_root,
            )
            exposure_symbols = set(account_exposure_labels(route=route))
        else:
            # First provision: no account exists yet, so no exposure exists.
            print(
                json.dumps(
                    {
                        "note": "held-exposure account root does not exist yet; "
                        "treating exposure as empty",
                        "account_root": str(account_root),
                    },
                    sort_keys=True,
                )
            )

    observed_ts_ns = time.time_ns()
    optional_symbols = exposure_symbols - universe_symbols
    rules = build_venue_instrument_rules(
        client,
        realm=realm,
        symbols=universe_symbols | exposure_symbols,
        observed_ts_ns=observed_ts_ns,
        optional_symbols=optional_symbols,
    )

    held_exposure: dict[str, dict[str, str]] = {
        symbol: {"basis": "live_instruments_info"}
        for symbol in sorted(optional_symbols & set(rules))
    }
    unruled = sorted(optional_symbols - set(rules))
    if unruled and args.prior_rules_file:
        prior_path = Path(args.prior_rules_file).expanduser()
        if prior_path.exists():
            prior_bytes = prior_path.read_bytes()
            prior_rules = load_venue_rules_bytes(
                prior_bytes,
                realm=realm,
                max_age_seconds=None,
            )
            prior_sha256 = hashlib.sha256(prior_bytes).hexdigest()
            for symbol in list(unruled):
                carried = prior_rules.get(symbol)
                if carried is not None:
                    rules[symbol] = carried
                    held_exposure[symbol] = {
                        "basis": "prior_receipt_carryover",
                        "source_receipt_sha256": prior_sha256,
                    }
                    unruled.remove(symbol)
    if unruled:
        # Exposure with no rule anywhere: its exits cannot be built until the
        # venue settles the position. Loud, not fatal — failing here would
        # block the renewal for every other symbol.
        print(
            json.dumps(
                {
                    "warning": "held-exposure symbols have no live or prior rule",
                    "held_exposure_unruled": unruled,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )

    data = render_venue_rules_artifact(
        rules,
        realm=realm,
        verified_ts_ns=observed_ts_ns,
        symbol_source=symbol_source,
        held_exposure=held_exposure,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(output), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print(
        json.dumps(
            {
                "realm": realm.value,
                "symbols": len(rules),
                "held_exposure_symbols": sorted(held_exposure),
                "held_exposure_unruled": unruled,
                "evidence": "venue_declared",
                "output": str(output),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
