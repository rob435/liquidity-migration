#!/usr/bin/env python3
"""Fail-closed check that the configured Bybit demo key can submit orders."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from liquidity_migration.bybit import (  # noqa: E402
    BybitPrivateClient,
    api_key_allows_order_submit,
    resolve_private_credentials,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", default="verify", help="short label for error messages")
    args = parser.parse_args()

    api_key, api_secret, demo = resolve_private_credentials()
    if not demo:
        print(
            f"{args.context}: refusing order-permission check on REAL_MONEY credentials; "
            "this repo is demo/paper by default.",
            file=sys.stderr,
        )
        return 1
    if not api_key or not api_secret:
        print(
            f"{args.context}: missing BYBIT_DEMO_API_KEY/BYBIT_DEMO_API_SECRET; "
            "order-submitting demo services cannot be verified.",
            file=sys.stderr,
        )
        return 1

    client = BybitPrivateClient(api_key=api_key, api_secret=api_secret, demo=True)
    info = client.get_api_key_information()
    allowed, reason = api_key_allows_order_submit(info)
    if not allowed:
        print(
            f"{args.context}: configured Bybit demo API key cannot submit orders: {reason}. "
            "Replace /etc/liquidity-migration/bybit-demo.env with a non-read-only demo key "
            "before relying on order-submitting services.",
            file=sys.stderr,
        )
        return 1

    print("bybit-order-permissions-ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
