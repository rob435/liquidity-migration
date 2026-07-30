#!/usr/bin/env python3
"""Freeze instrument rules from Bybit's read-only instruments-info endpoint.

This is the off-demo replacement for ``probe_bybit_demo_rules.py``, which finds
demo's empirical minimum notional by submitting and cancelling real PostOnly
orders up to 200 USDT per symbol. On a funded account that technique makes a
deploy spend money, so it is refused there outright (B17) and this script reads
the venue's declared rules instead.

It places no orders. ``get_instruments_info`` is an authenticated read-only
call: it needs no mutation lease and creates no exposure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from liquidity_migration.bybit import (  # noqa: E402
    BybitPrivateClient,
    resolve_private_credentials,
)
from liquidity_migration.logging_setup import ensure_default_log_handler  # noqa: E402
from liquidity_migration.venue_instrument_rules import (  # noqa: E402
    build_venue_instrument_rules,
    render_venue_rules_artifact,
)
from liquidity_migration.venue_realm import (  # noqa: E402
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
    parser.add_argument("--symbols-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    realm = venue_realm(args.realm)
    output = Path(args.output).expanduser()
    if output.exists():
        parser.error(f"refusing to overwrite an existing rules artifact: {output}")

    api_key, api_secret = resolve_private_credentials(realm=realm)
    if not api_key or not api_secret:
        key_variable, secret_variable = REALM_CREDENTIAL_VARIABLES[realm]
        parser.error(f"{key_variable} and {secret_variable} are required")

    client = BybitPrivateClient(
        category="linear",
        testnet=False,
        demo=realm is VenueRealm.DEMO,
        realm=realm,
        api_key=api_key,
        api_secret=api_secret,
        # No mutation lease on purpose: every call this script makes is a read.
    )
    observed_ts_ns = time.time_ns()
    rules = build_venue_instrument_rules(
        client,
        realm=realm,
        symbols=_symbols(Path(args.symbols_file).expanduser()),
        observed_ts_ns=observed_ts_ns,
    )
    data = render_venue_rules_artifact(rules, realm=realm, verified_ts_ns=observed_ts_ns)
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
