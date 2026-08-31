"""Python reader for the shared operational sizing and risk configuration.

Native Rust strategy configuration and Python reference tooling read the same
profile. It is strictly operational: changing it does not promote research
evidence, and the Rust engine remains the final risk authority.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from liquidity_migration.core.artifact_snapshot import StableFileSnapshot, read_stable_file


OPERATIONAL_PROFILE_SCHEMA_VERSION = 2
OPERATIONAL_PROFILE_KIND = "liquidity_migration_operational_profile"


def _object(
    value: object,
    *,
    label: str,
    fields: set[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    """Reject typos without forcing every profile to restate every new knob.

    ``fields`` are mandatory; ``optional`` names permitted-but-not-required keys
    so a new control does not invalidate an already-deployed profile. Unknown
    keys raise, turning a typo into a startup failure instead of an ignored limit.
    """

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    missing = sorted(fields - set(value))
    unknown = sorted(set(value) - fields - optional)
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(missing)}")
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(unknown)}")
    return value


def _positive_float(value: object, *, label: str, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        output = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    minimum_ok = output >= 0.0 if allow_zero else output > 0.0
    if not math.isfinite(output) or not minimum_ok:
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{label} must be finite and {qualifier}")
    return output


def _positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class AccountRiskSettings:
    max_component_gross_notional_usdt: float
    max_account_gross_notional_usdt: float
    max_initial_margin_usdt: float
    max_leverage: float
    quantity_tolerance: float


@dataclass(frozen=True, slots=True)
class LongOperationalSettings:
    notional_multiplier: float
    entry_leverage: float
    order_notional_pct_equity: float
    max_new_entries_per_cycle: int


@dataclass(frozen=True, slots=True)
class HedgeOperationalSettings:
    entry_leverage: float


@dataclass(frozen=True, slots=True)
class CarryOperationalSettings:
    """Operational envelope for the CARRY sleeve.

    Rule parameters (entry/exit prints, filters, per-name and gross caps) come
    from the registered config the native reducer loads. This block carries only the
    deployment dials. The declared stop fraction is deliberately wide, so the
    strategy's funding-normalization exit is always the real exit.
    """

    notional_multiplier: float
    entry_leverage: float
    declared_stop_loss_fraction: float
    max_new_entries_per_cycle: int


CAPITAL_REFERENCE_FIXED = "fixed"
CAPITAL_REFERENCE_ACCOUNT_EQUITY = "account_equity"


@dataclass(frozen=True, slots=True)
class CapitalReferenceSettings:
    """How the profile's capital reference is chosen at runtime.

    ``fixed`` (default)
        The declared ``capital_reference_usdt`` is the reference forever.
    ``account_equity``
        The reference tracks observed venue equity. Every envelope and cap here
        is linear in the reference, but the load-time proof is still re-run at
        each rebase because ``max_leverage`` and ``quantity_tolerance`` are not.

    ``floor_usdt`` bounds the reference from below so an unreadable or near-zero
    balance cannot produce a degenerate envelope. The dead band applies to
    expansion only: contraction follows equity down immediately.
    """

    mode: str = CAPITAL_REFERENCE_FIXED
    equity_fraction: float = 1.0
    floor_usdt: float = 0.0
    expand_dead_band_fraction: float = 0.05

    @property
    def tracks_equity(self) -> bool:
        return self.mode == CAPITAL_REFERENCE_ACCOUNT_EQUITY


@dataclass(frozen=True, slots=True)
class OperationalProfile:
    capital_reference_usdt: float
    account_risk: AccountRiskSettings
    long: LongOperationalSettings
    carry: CarryOperationalSettings
    hedge: HedgeOperationalSettings
    source_sha256: str
    source_path: Path | None = None
    schema_version: int = OPERATIONAL_PROFILE_SCHEMA_VERSION
    kind: str = OPERATIONAL_PROFILE_KIND
    capital_reference: CapitalReferenceSettings = CapitalReferenceSettings()


def _parse_account_risk(value: object) -> AccountRiskSettings:
    fields = {
        "max_component_gross_notional_usdt",
        "max_account_gross_notional_usdt",
        "max_initial_margin_usdt",
        "max_leverage",
        "quantity_tolerance",
    }
    row = _object(value, label="operational profile account_risk", fields=fields)
    settings = AccountRiskSettings(
        max_component_gross_notional_usdt=_positive_float(
            row["max_component_gross_notional_usdt"],
            label="account_risk.max_component_gross_notional_usdt",
        ),
        max_account_gross_notional_usdt=_positive_float(
            row["max_account_gross_notional_usdt"],
            label="account_risk.max_account_gross_notional_usdt",
        ),
        max_initial_margin_usdt=_positive_float(
            row["max_initial_margin_usdt"],
            label="account_risk.max_initial_margin_usdt",
        ),
        max_leverage=_positive_float(row["max_leverage"], label="account_risk.max_leverage"),
        quantity_tolerance=_positive_float(row["quantity_tolerance"], label="account_risk.quantity_tolerance"),
    )
    if settings.max_component_gross_notional_usdt > settings.max_account_gross_notional_usdt:
        raise ValueError("account_risk component cap cannot exceed the account cap")
    return settings


def _parse_long(value: object) -> LongOperationalSettings:
    fields = {
        "notional_multiplier",
        "entry_leverage",
        "order_notional_pct_equity",
        "max_new_entries_per_cycle",
    }
    row = _object(value, label="operational profile long", fields=fields)
    order_cap = _positive_float(
        row["order_notional_pct_equity"],
        label="long.order_notional_pct_equity",
        allow_zero=True,
    )
    if order_cap > 10.0:
        raise ValueError("long.order_notional_pct_equity cannot exceed 10")
    return LongOperationalSettings(
        notional_multiplier=_positive_float(row["notional_multiplier"], label="long.notional_multiplier"),
        entry_leverage=_positive_float(row["entry_leverage"], label="long.entry_leverage"),
        order_notional_pct_equity=order_cap,
        max_new_entries_per_cycle=_positive_int(
            row["max_new_entries_per_cycle"],
            label="long.max_new_entries_per_cycle",
        ),
    )


def _parse_carry(value: object) -> CarryOperationalSettings:
    fields = {
        "notional_multiplier",
        "entry_leverage",
        "declared_stop_loss_fraction",
        "max_new_entries_per_cycle",
    }
    row = _object(value, label="operational profile carry", fields=fields)
    stop_fraction = _positive_float(row["declared_stop_loss_fraction"], label="carry.declared_stop_loss_fraction")
    if not (0.0 < stop_fraction < 1.0):
        raise ValueError("carry.declared_stop_loss_fraction must sit in (0, 1)")
    return CarryOperationalSettings(
        notional_multiplier=_positive_float(row["notional_multiplier"], label="carry.notional_multiplier"),
        entry_leverage=_positive_float(row["entry_leverage"], label="carry.entry_leverage"),
        declared_stop_loss_fraction=stop_fraction,
        max_new_entries_per_cycle=_positive_int(
            row["max_new_entries_per_cycle"],
            label="carry.max_new_entries_per_cycle",
        ),
    )


def _parse_hedge(value: object) -> HedgeOperationalSettings:
    row = _object(
        value,
        label="operational profile hedge",
        fields={"entry_leverage"},
    )
    return HedgeOperationalSettings(entry_leverage=_positive_float(row["entry_leverage"], label="hedge.entry_leverage"))


def _validate_profile_envelopes(profile: OperationalProfile) -> None:
    """Refuse structurally impossible profiles, before any sizing opinion.

    What remains here is arithmetic self-consistency: a strategy may not ask
    for leverage the account forbids, and the account caps must nest inside
    what the capital reference could fund. How large a book the multipliers
    build is the owner's dial, not a load-time refusal — per-position risk is
    bounded by each position's own venue-native stop.
    """

    risk = profile.account_risk
    # Dials may pin a cap exactly at its bound (gross cap = reference * max
    # leverage, margin cap = reference), and every equity rebase re-derives the
    # absolutes by multiplication, so each comparison carries a rounding
    # allowance far below economic size.
    tolerance = max(1e-9, profile.capital_reference_usdt * 1e-12)
    leverage_requests = {
        "long": profile.long.entry_leverage,
        "carry": profile.carry.entry_leverage,
        "hedge": profile.hedge.entry_leverage,
    }
    excessive = sorted(name for name, leverage in leverage_requests.items() if leverage > risk.max_leverage)
    if excessive:
        raise ValueError("strategy leverage exceeds account_risk.max_leverage: " + ", ".join(excessive))
    if risk.max_account_gross_notional_usdt > profile.capital_reference_usdt * risk.max_leverage + tolerance:
        raise ValueError("account_risk account gross cap exceeds capital_reference_usdt * max_leverage")
    if risk.max_initial_margin_usdt > profile.capital_reference_usdt + tolerance:
        raise ValueError("account_risk initial-margin cap exceeds capital_reference_usdt")


def _parse_capital_reference(value: object) -> CapitalReferenceSettings:
    row = _object(
        value,
        label="operational profile capital_reference",
        fields={"mode"},
        optional=frozenset({"equity_fraction", "floor_usdt", "expand_dead_band_fraction"}),
    )
    mode = str(row["mode"])
    if mode not in {CAPITAL_REFERENCE_FIXED, CAPITAL_REFERENCE_ACCOUNT_EQUITY}:
        raise ValueError(
            f"capital_reference.mode must be {CAPITAL_REFERENCE_FIXED!r} or {CAPITAL_REFERENCE_ACCOUNT_EQUITY!r}"
        )
    equity_fraction = _positive_float(row.get("equity_fraction", 1.0), label="capital_reference.equity_fraction")
    if equity_fraction > 1.0:
        # The reference is a ceiling on the book, so a fraction above 1 would
        # authorize an envelope larger than the wallet backing it.
        raise ValueError("capital_reference.equity_fraction cannot exceed 1")
    dead_band = _positive_float(
        row.get("expand_dead_band_fraction", 0.05),
        label="capital_reference.expand_dead_band_fraction",
        allow_zero=True,
    )
    if dead_band >= 1.0:
        raise ValueError("capital_reference.expand_dead_band_fraction must be below 1")
    floor = _positive_float(
        row.get("floor_usdt", 0.0),
        label="capital_reference.floor_usdt",
        allow_zero=True,
    )
    if mode == CAPITAL_REFERENCE_ACCOUNT_EQUITY and floor <= 0.0:
        # Without a floor, an unreadable or near-zero balance produces a
        # degenerate envelope rather than a refusal.
        raise ValueError("capital_reference.floor_usdt must be positive in account_equity mode")
    return CapitalReferenceSettings(
        mode=mode,
        equity_fraction=equity_fraction,
        floor_usdt=floor,
        expand_dead_band_fraction=dead_band,
    )


def load_operational_profile_bytes(
    data: bytes,
    *,
    source_path: Path | None = None,
) -> OperationalProfile:
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("operational profile is not valid UTF-8 JSON") from exc
    fields = {
        "schema_version",
        "kind",
        "capital_reference_usdt",
        "account_risk",
        "long",
        "carry",
        "hedge",
    }
    row = _object(
        payload,
        label="operational profile",
        fields=fields,
        # Absent ``capital_reference`` means a fixed reference.
        optional=frozenset({"capital_reference"}),
    )
    if row["schema_version"] != OPERATIONAL_PROFILE_SCHEMA_VERSION:
        raise ValueError("operational profile schema_version is unsupported")
    if row["kind"] != OPERATIONAL_PROFILE_KIND:
        raise ValueError("operational profile kind is invalid")
    profile = OperationalProfile(
        capital_reference_usdt=_positive_float(row["capital_reference_usdt"], label="capital_reference_usdt"),
        account_risk=_parse_account_risk(row["account_risk"]),
        long=_parse_long(row["long"]),
        carry=_parse_carry(row["carry"]),
        hedge=_parse_hedge(row["hedge"]),
        source_sha256=hashlib.sha256(data).hexdigest(),
        source_path=source_path,
        capital_reference=(
            _parse_capital_reference(row["capital_reference"])
            if row.get("capital_reference") is not None
            else CapitalReferenceSettings()
        ),
    )
    _validate_profile_envelopes(profile)
    if (
        profile.capital_reference.tracks_equity
        and profile.capital_reference.floor_usdt > profile.capital_reference_usdt
    ):
        raise ValueError("capital_reference.floor_usdt cannot exceed capital_reference_usdt")
    return profile


def load_operational_profile(
    path: str | Path,
    *,
    snapshot: StableFileSnapshot | None = None,
) -> OperationalProfile:
    if snapshot is None:
        snapshot = read_stable_file(
            path,
            label="operational profile",
            require_single_link=False,
        )
    elif snapshot.path != Path(path).expanduser().absolute():
        raise ValueError("operational profile snapshot path differs")
    return load_operational_profile_bytes(snapshot.data, source_path=snapshot.path)
