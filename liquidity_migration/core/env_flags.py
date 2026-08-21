"""Canonical semantics for environment values the whole program reads.

Several layers validate the same toggle or identifier independently; all of
them import from here so an operator value is interpreted identically
everywhere.
"""

from __future__ import annotations

import os
from typing import Mapping


TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})


def env_flag(name: str, *, environ: Mapping[str, str] | None = None) -> bool:
    """True when ``name`` is set to an explicitly truthy value.

    Ambiguous values are NOT truthy; pair with :func:`reject_ambiguous_flag`
    when a typo should fail startup rather than coerce to the default.
    """

    source = os.environ if environ is None else environ
    return source.get(name, "").strip().lower() in TRUE_ENV_VALUES




def reject_ambiguous_flag(name: str, *, environ: Mapping[str, str] | None = None) -> None:
    """Raise if ``name`` is set to a value that is neither clearly true nor
    clearly false, so a typo surfaces at startup instead of coercing."""

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


def env_positive_float(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> float | None:
    """The named dial as a positive finite float, or None when unset.

    An empty or malformed value raises rather than falling back: an operator
    who typed a line meant to change the number, and a sizing dial that
    silently reverts to the committed default is a size nobody chose. Same
    rule as ``parse_real_money_dials`` applies to the ``RM_*`` surface.
    """

    source = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        raise ValueError(f"{name} is present but empty; remove the line to take the default")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}") from exc
    # NaN fails `> 0.0` on its own, so this is finiteness against the infinities.
    if not value > 0.0 or value == float("inf"):
        raise ValueError(f"{name} must be finite and positive; got {raw!r}")
    return value


def validate_systemd_invocation_id(value: object, *, label: str = "systemd INVOCATION_ID") -> str:
    """Return one canonical non-zero systemd invocation identifier."""

    if type(value) is not str or len(value) != 32 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{label} must be exactly 32 lowercase hexadecimal characters")
    if value == "0" * 32:
        raise ValueError(f"{label} cannot be the zero identifier")
    return value
