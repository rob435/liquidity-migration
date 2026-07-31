"""Turn completed-run integrity signals into actionable warnings.

Each :class:`RunWarning` carries

* ``code``      — a stable, greppable identifier (``FUNDING_MISSING``, ...)
* ``severity``  — ``info`` < ``warn`` < ``tainted``
* ``message``   — one human sentence
* ``fix``       — the one-line backfill/command that clears it (``""`` if none)

Any ``tainted``-severity warning means the result is survivorship or look-ahead
biased and must not be cited as clean. The failure taxonomy lives in
``docs/backtesting_errors_we_never_repeat.md``. Data-gap warnings (funding,
clipped window) are ``warn``/``info``: the run is produced and the gap named.
"""
from __future__ import annotations

from dataclasses import dataclass

INFO = "info"
WARN = "warn"
TAINTED = "tainted"

_SEVERITY_RANK = {INFO: 0, WARN: 1, TAINTED: 2}
_SEVERITY_ICON = {INFO: "ℹ", WARN: "⚠", TAINTED: "⛔"}


@dataclass(frozen=True)
class RunWarning:
    code: str
    severity: str
    message: str
    fix: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "severity": self.severity, "message": self.message, "fix": self.fix}


def diagnose(
    *,
    full_pit_universe_pass: bool,
    funding_mode: str,
    archive_manifest_empty: bool,
    requested_start: str | None = None,
    requested_end: str | None = None,
    data_start: str | None = None,
    data_end: str | None = None,
    n_features: int = 0,
    n_trades: int = 0,
) -> list[RunWarning]:
    """Build the warning list for a completed run from its integrity signals."""
    warnings: list[RunWarning] = []

    # --- correctness / tainting ---
    if archive_manifest_empty:
        warnings.append(
            RunWarning(
                "PIT_MANIFEST_EMPTY",
                TAINTED,
                "PIT membership manifest is empty — the universe cannot be reconstructed "
                "point-in-time, so results are survivorship-biased and not citable as clean.",
                "Rebuild the manifest for the affected root, then re-run the exact "
                "research command: `python -m liquidity_migration --data-root ROOT "
                "archive-manifest`",
            )
        )
    elif not full_pit_universe_pass:
        warnings.append(
            RunWarning(
                "PIT_SURVIVORSHIP",
                TAINTED,
                "Full-PIT universe gate FAILED — the run fell back to the CURRENT universe, "
                "so delisted/renamed/prelisted names are missing (survivorship bias). "
                "Not citable as clean evidence.",
                "Rebuild membership with `python -m liquidity_migration --data-root ROOT "
                "archive-manifest`, close the named kline gap for that root, and inspect "
                "each command's current `--help` before choosing dates.",
            )
        )

    # --- funding (data gap: marked, not blocked) ---
    if funding_mode == "missing":
        warnings.append(
            RunWarning(
                "FUNDING_MISSING",
                WARN,
                "Funding cost is NOT charged — no funding data overlaps the traded "
                "symbols/window. Returns are optimistic on cost.",
                "Backfill funding for this root: "
                "`python -m liquidity_migration download-data --datasets funding`",
            )
        )
    elif funding_mode == "partial":
        warnings.append(
            RunWarning(
                "FUNDING_PARTIAL",
                WARN,
                "Funding only partially covers the traded symbols/window — some funding "
                "cost is uncharged, so returns are mildly optimistic on cost.",
                "Backfill the missing funding symbols/dates: "
                "`python -m liquidity_migration download-data --datasets funding`",
            )
        )

    # --- window coverage (informational: tell the user what was actually run) ---
    if requested_start and data_start and requested_start < data_start:
        warnings.append(
            RunWarning(
                "WINDOW_CLIPPED_START",
                INFO,
                f"Requested start {requested_start} precedes the earliest data "
                f"{data_start} on this root — the window was clipped to available history.",
                "Backfill earlier history if you need it: "
                "`python -m liquidity_migration download-data --datasets klines_1h`",
            )
        )
    if requested_end and data_end and requested_end > data_end:
        warnings.append(
            RunWarning(
                "WINDOW_CLIPPED_END",
                INFO,
                f"Requested end {requested_end} is after the latest data {data_end} "
                "on this root — the window was clipped to available history.",
                "Refresh recent data: `python -m liquidity_migration download-data`",
            )
        )

    # --- empties ---
    if n_features == 0:
        warnings.append(
            RunWarning(
                "NO_FEATURES",
                WARN,
                "No feature rows were generated for the window — nothing to trade.",
                "Check the date window and that klines_1h covers it.",
            )
        )
    elif n_trades == 0:
        warnings.append(
            RunWarning(
                "NO_TRADES",
                WARN,
                "Features were built but no entry fired in the window — zero trades.",
                "",
            )
        )

    return warnings


def is_tainted(warnings: list[RunWarning]) -> bool:
    """True when any warning marks the result survivorship/look-ahead biased."""
    return any(w.severity == TAINTED for w in warnings)


def render(warnings: list[RunWarning], *, title: str = "backtest") -> str:
    """A compact, loud block for stdout. Empty list → a single clean line."""
    if not warnings:
        return f"✅ {title}: no warnings (clean run)."
    head = "⛔ TAINTED" if is_tainted(warnings) else "⚠ warnings"
    lines = [f"{head} — {title}:"]
    # tainted first, then warn, then info
    for w in sorted(warnings, key=lambda w: -_SEVERITY_RANK.get(w.severity, 0)):
        icon = _SEVERITY_ICON.get(w.severity, "•")
        lines.append(f"  {icon} [{w.code}] {w.message}")
        if w.fix:
            lines.append(f"      ↳ fix: {w.fix}")
    return "\n".join(lines)
