"""Where the engine still turns a venue decimal into a float, and nowhere else.

The engine is mid-migration to exact decimals. Fees, order terms, instrument
grids, positions, account balances and the WAL are exact; a few float-shaped
structures beside them are not yet, and each one is fed by
``DecimalField::compat_f64``, which throws the venue's own decimal away.

Every one of those calls is listed here. The list is not a style rule — it is
the migration's remaining surface, written down so that adding to it is a
deliberate line in a diff rather than the path of least resistance, and so
that removing one is visibly progress.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: file -> the fields read as floats there, and why that is still correct.
COMPAT_F64_CALLS: dict[str, set[str]] = {
    # MEXC prices in whole contracts, and the contract-count arithmetic
    # (`quantize::steps`, `round_clean`) is float. The exact grid for the same
    # instrument is `Contract.exact_spec`, which is what orders quantize
    # against; these feed the contract-count conversion only.
    "engine/engine-public/src/venues/mexc/contracts.rs": {
        "contractSize",
        "priceUnit",
        "maxVol",
        "minVol",
        "limitMaxVol",
    },
    # A funding interval in hours and a settlement stamp: a schedule, not money.
    "engine/engine-public/src/venues/mexc/public.rs": {"collectCycle", "nextSettleTime"},
    # Fill projections. Every one of these executions also carries the venue's
    # own decimals in `ExecutionAmounts`, and accounting reads those; the float
    # beside them is the compatibility projection the WAL's older records use.
    "engine/engine-venue/src/venues/binance/execution.rs": {"l", "L"},
    "engine/engine-venue/src/venues/bybit/execution.rs": {"execQty", "execPrice"},
    "engine/engine-venue/src/venues/hyperliquid/execution.rs": {"sz", "px", "fee"},
    "engine/engine-venue/src/venues/lighter/execution.rs": {"size", "price"},
    "engine/engine-venue/src/venues/mexc/execution.rs": {"price"},
}

CALL = re.compile(r"\.compat_f64\(\s*\"([^\"]+)\"")


def _calls() -> dict[str, set[str]]:
    listed = subprocess.run(
        ["git", "grep", "-n", "compat_f64(", "--", "engine/*.rs", "engine/**/*.rs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    found: dict[str, set[str]] = {}
    for line in listed:
        path, _, rest = line.partition(":")
        if path.endswith("numeric_wire.rs"):
            continue  # the definition and its own tests
        fields = CALL.findall(rest)
        if fields:
            found.setdefault(path, set()).update(fields)
    return found


def test_the_float_compatibility_surface_is_exactly_what_is_written_down() -> None:
    found = _calls()
    assert found, "no compat_f64 call sites found; has the accessor been renamed?"

    new_files = sorted(set(found) - set(COMPAT_F64_CALLS))
    assert not new_files, (
        "a new file turns venue decimals into floats: "
        + ", ".join(new_files)
        + ". If that is right, add it to COMPAT_F64_CALLS with the reason it is still correct."
    )

    gone = sorted(set(COMPAT_F64_CALLS) - set(found))
    assert not gone, (
        "these files no longer use the float compatibility path, which is progress: "
        + ", ".join(gone)
        + ". Remove them from COMPAT_F64_CALLS."
    )

    for path, expected in sorted(COMPAT_F64_CALLS.items()):
        actual = found[path]
        assert actual == expected, (
            f"{path} reads {sorted(actual)} as floats, not {sorted(expected)}. "
            "A field added here is a venue decimal being discarded; a field removed is progress."
        )


def test_the_engine_cannot_build_an_order_without_the_terms_it_quantized_from() -> None:
    """The venue adapters still encode an order with no exact terms; the engine cannot produce one.

    Both halves matter. The adapters' own wire-contract suite (``request_shape``,
    ``conformance``, ``venue_registry``) is built on requests without exact
    terms, and ``engine-wal`` has a record shape for an amendment without them,
    so the float formatter at the venue boundary is live code with live tests.
    What makes it unreachable from the engine is this: a symbol whose exact
    instrument metadata the engine does not hold is refused, and the one
    remaining path that builds an order without terms is compiled only into a
    test binary. If that ``#[cfg(test)]`` ever comes off, a float-priced order
    becomes reachable in a release build, and this says so.
    """

    source = (ROOT / "engine/engine-core/src/engine/intent_admission.rs").read_text(encoding="utf-8")

    marker = "fn quantize_legacy_order_fixture("
    assert marker in source, "the legacy order fixture was renamed; re-derive this contract"

    # The refusal a symbol without exact metadata gets.
    assert "EXACT_INSTRUMENT_METADATA_UNAVAILABLE" in source

    # Every mention of the fixture — its definition and its one call — sits
    # under a #[cfg(test)] on the line above it.
    lines = source.splitlines()
    sites = [index for index, line in enumerate(lines) if "quantize_legacy_order_fixture" in line]
    assert len(sites) == 2, f"expected a definition and one call, found {len(sites)}"
    for index in sites:
        preceding = [line.strip() for line in lines[max(0, index - 3) : index]]
        assert "#[cfg(test)]" in preceding, (
            f"line {index + 1} builds an order without exact terms outside a test build: "
            f"{lines[index].strip()}"
        )


def test_nothing_outside_the_compatibility_accessor_reaches_for_a_wire_float() -> None:
    # `legacy` was the old name and said nothing about its cost. Nothing should
    # bring it back, under that name or as a second accessor beside it.
    listed = subprocess.run(
        ["git", "grep", "-n", r"fn legacy(\|\.legacy(", "--", "engine/*.rs", "engine/**/*.rs"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert not listed, "a `legacy()` wire accessor is back:\n" + listed
