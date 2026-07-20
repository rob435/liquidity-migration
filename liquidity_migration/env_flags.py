"""Canonical boolean environment-flag semantics.

Safety-critical toggles (``REAL_MONEY`` above all) are validated independently
at several fail-closed layers by design — credential resolution, paper-runtime
isolation, operational-authority issuance, and runtime verification each keep
their own check.  What must never diverge between those layers is the meaning
of "true" and "false": every layer imports these sets and helpers so an
operator value is interpreted identically everywhere.
"""

from __future__ import annotations

import os
from typing import Mapping


TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})


def env_flag(name: str, *, environ: Mapping[str, str] | None = None) -> bool:
    """True when ``name`` is set to an explicitly truthy value.

    Ambiguous values are NOT truthy; pair with :func:`reject_ambiguous_flag`
    when a typo'd high-stakes toggle must fail startup instead of silently
    coercing to the default.
    """

    source = os.environ if environ is None else environ
    return source.get(name, "").strip().lower() in TRUE_ENV_VALUES


def explicitly_false_or_unset(value: str | None) -> bool:
    """True when ``value`` is unset or an explicit false spelling.

    This is the fail-closed predicate for destructive-capability flags: an
    ambiguous value is treated as NOT explicitly disabled, so callers refuse
    to proceed.
    """

    return (value or "").strip().lower() in FALSE_ENV_VALUES


def reject_ambiguous_flag(name: str, *, environ: Mapping[str, str] | None = None) -> None:
    """Raise if ``name`` is set to a value that is neither clearly true nor
    clearly false. Keeps the fail-safe direction (an unrecognised value never
    silently means 'on'), but surfaces a typo'd high-stakes toggle at startup
    instead of letting it coerce to the default."""

    source = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None:
        return
    normalised = raw.strip().lower()
    if normalised in TRUE_ENV_VALUES or normalised in FALSE_ENV_VALUES:
        return
    raise RuntimeError(
        f"{name}={raw!r} is not a recognised boolean. Use one of "
        f"{sorted(TRUE_ENV_VALUES)} to enable or {sorted(FALSE_ENV_VALUES - {''})} "
        f"(or unset) to disable -- refusing to guess for a safety-critical toggle."
    )
