"""Named, backfillable backtest warnings — the single place that turns a run's
integrity signals into loud, actionable messages.

Why this exists
---------------
For a long time a backtest's health was encoded in one opaque ``run_label``
string (e.g. ``pit_membership_filtered_current_universe``) that you had to decode
by hand against a skill doc, plus a separate ``funding_mode`` buried in the
summary. Two failure modes followed: a run either hard-aborted on missing data,
or it silently handed back a number that was survivorship-biased. Neither told
you *what* was wrong or *how to fix it*.

This module replaces that with a flat list of :class:`RunWarning`s. Each carries

* ``code``      — a stable, greppable identifier (``FUNDING_MISSING``, ...)
* ``severity``  — ``info`` < ``warn`` < ``tainted``
* ``message``   — one human sentence
* ``fix``       — the one-line backfill/command that clears it (``""`` if none)

``tainted`` (any ``tainted``-severity warning) is the machine flag meaning
"this result is survivorship / look-ahead biased — do NOT cite it as clean."
That implements the claim-validity policy in ``docs/governance.md``; the
failure taxonomy lives in ``docs/backtesting_errors_we_never_repeat.md``.
Here it is surfaced loudly rather than hidden. Data-gap warnings (funding, clipped window)
are ``warn``/``info``: the run is still produced, the gap is named, and you can
backfill it yourself.

The legacy ``run_label`` is still emitted alongside these warnings for the
decision tooling that keys on it; this module is the human-facing surface.
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
    """Build the warning list for a completed run from its integrity signals.

    Pure and side-effect free so it is trivially unit-testable; callers attach
    the result to the report payload and render it to stdout.
    """
    warnings: list[RunWarning] = []

    # --- correctness / tainting (claim-validity gate, never silently dropped) ---
    if archive_manifest_empty:
        warnings.append(
            RunWarning(
                "PIT_MANIFEST_EMPTY",
                TAINTED,
                "PIT membership manifest is empty — the universe cannot be reconstructed "
                "point-in-time, so results are survivorship-biased and not citable as clean.",
                "Backfill the manifest, then re-run: bash scripts/reconcile.sh",
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
                "Close the kline/manifest coverage gap: bash scripts/reconcile.sh, then "
                "`python -m liquidity_migration archive-download-klines-1h` for the gap dates.",
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


def max_severity(warnings: list[RunWarning]) -> str:
    if not warnings:
        return INFO
    return max((w.severity for w in warnings), key=lambda s: _SEVERITY_RANK.get(s, 0))


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
