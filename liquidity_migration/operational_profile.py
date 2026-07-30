"""Single-source operational sizing and account-risk configuration.

The profile is shared by every target producer and the account owner.  It is
strictly operational: changing it does not promote research evidence, and the
account owner remains the final authority for every risk-increasing target.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .account_contracts import AccountRiskPolicy
from .artifact_snapshot import StableFileSnapshot, read_stable_file


OPERATIONAL_PROFILE_SCHEMA_VERSION = 1
OPERATIONAL_PROFILE_KIND = "liquidity_migration_operational_profile"


def _object(
    value: object,
    *,
    label: str,
    fields: set[str],
    optional: frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    """Reject typos without forcing every profile to restate every new knob.

    ``fields`` stay mandatory: a profile missing one is a misconfiguration, and
    the unknown-field check is what turns a typo into a startup failure instead
    of a silently ignored limit. ``optional`` names keys that are permitted but
    not required, so a control can be added without invalidating the profile
    already deployed -- which would otherwise stop the owner from starting.
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
    max_symbol_notional_usdt: float
    max_initial_margin_usdt: float
    max_leverage: float
    quantity_tolerance: float
    max_daily_loss_usdt: float = 0.0

    def to_policy(self) -> AccountRiskPolicy:
        return AccountRiskPolicy(
            max_component_gross_notional_usdt=self.max_component_gross_notional_usdt,
            max_account_gross_notional_usdt=self.max_account_gross_notional_usdt,
            max_symbol_notional_usdt=self.max_symbol_notional_usdt,
            max_initial_margin_usdt=self.max_initial_margin_usdt,
            max_leverage=self.max_leverage,
            quantity_tolerance=self.quantity_tolerance,
            max_daily_loss_usdt=self.max_daily_loss_usdt,
        )


@dataclass(frozen=True, slots=True)
class LongOperationalSettings:
    notional_multiplier: float
    entry_leverage: float
    max_projected_initial_margin_pct_equity: float
    max_order_notional_pct_equity: float
    max_new_entries_per_cycle: int


@dataclass(frozen=True, slots=True)
class ContinuousOperationalSettings:
    max_active: int
    max_new_entries_per_cycle: int
    btc_trend_gate: str
    entry_leverage: float
    notional_multiplier: float
    per_position_notional_pct_equity: float


@dataclass(frozen=True, slots=True)
class HedgeOperationalSettings:
    entry_leverage: float


@dataclass(frozen=True, slots=True)
class CarryOperationalSettings:
    """Operational envelope for the CARRY sleeve.

    Rule parameters (entry/exit prints, filters, per-name cap, gross cap)
    come from the immutable registered config the producer loads; this block
    carries only the deployment dials: sizing multiplier, entry leverage,
    the declared disaster-stop fraction (the sl35 pattern — wide enough that
    the strategy's funding-normalization exit is always the real exit), and
    the per-cycle entry throttle.
    """

    notional_multiplier: float
    entry_leverage: float
    declared_stop_loss_fraction: float
    max_new_entries_per_cycle: int


@dataclass(frozen=True, slots=True)
class OperationalProfile:
    capital_reference_usdt: float
    account_risk: AccountRiskSettings
    long: LongOperationalSettings
    continuous: ContinuousOperationalSettings
    carry: CarryOperationalSettings
    hedge: HedgeOperationalSettings
    source_sha256: str
    source_path: Path | None = None
    schema_version: int = OPERATIONAL_PROFILE_SCHEMA_VERSION
    kind: str = OPERATIONAL_PROFILE_KIND


def _parse_account_risk(value: object) -> AccountRiskSettings:
    fields = {
        "max_component_gross_notional_usdt",
        "max_account_gross_notional_usdt",
        "max_symbol_notional_usdt",
        "max_initial_margin_usdt",
        "max_leverage",
        "quantity_tolerance",
    }
    row = _object(
        value,
        label="operational profile account_risk",
        fields=fields,
        # Absent means no daily loss ceiling. Present and positive arms the
        # account-level halt. Optional so adding it cannot brick a deployed
        # profile that predates it.
        optional=frozenset({"max_daily_loss_usdt"}),
    )
    settings = AccountRiskSettings(
        max_component_gross_notional_usdt=_positive_float(
            row["max_component_gross_notional_usdt"],
            label="account_risk.max_component_gross_notional_usdt",
        ),
        max_account_gross_notional_usdt=_positive_float(
            row["max_account_gross_notional_usdt"],
            label="account_risk.max_account_gross_notional_usdt",
        ),
        max_symbol_notional_usdt=_positive_float(
            row["max_symbol_notional_usdt"],
            label="account_risk.max_symbol_notional_usdt",
        ),
        max_initial_margin_usdt=_positive_float(
            row["max_initial_margin_usdt"],
            label="account_risk.max_initial_margin_usdt",
        ),
        max_leverage=_positive_float(
            row["max_leverage"], label="account_risk.max_leverage"
        ),
        quantity_tolerance=_positive_float(
            row["quantity_tolerance"], label="account_risk.quantity_tolerance"
        ),
        max_daily_loss_usdt=(
            _positive_float(
                row["max_daily_loss_usdt"], label="account_risk.max_daily_loss_usdt"
            )
            if row.get("max_daily_loss_usdt") is not None
            else 0.0
        ),
    )
    if settings.max_symbol_notional_usdt > settings.max_component_gross_notional_usdt:
        raise ValueError("account_risk symbol cap cannot exceed the component cap")
    if settings.max_component_gross_notional_usdt > settings.max_account_gross_notional_usdt:
        raise ValueError("account_risk component cap cannot exceed the account cap")
    return settings


def _parse_long(value: object) -> LongOperationalSettings:
    fields = {
        "notional_multiplier",
        "entry_leverage",
        "max_projected_initial_margin_pct_equity",
        "max_order_notional_pct_equity",
        "max_new_entries_per_cycle",
    }
    row = _object(value, label="operational profile long", fields=fields)
    margin_fraction = _positive_float(
        row["max_projected_initial_margin_pct_equity"],
        label="long.max_projected_initial_margin_pct_equity",
    )
    if margin_fraction > 1.0:
        raise ValueError("long.max_projected_initial_margin_pct_equity cannot exceed 1")
    order_cap = _positive_float(
        row["max_order_notional_pct_equity"],
        label="long.max_order_notional_pct_equity",
        allow_zero=True,
    )
    if order_cap > 10.0:
        raise ValueError("long.max_order_notional_pct_equity cannot exceed 10")
    return LongOperationalSettings(
        notional_multiplier=_positive_float(
            row["notional_multiplier"], label="long.notional_multiplier"
        ),
        entry_leverage=_positive_float(
            row["entry_leverage"], label="long.entry_leverage"
        ),
        max_projected_initial_margin_pct_equity=margin_fraction,
        max_order_notional_pct_equity=order_cap,
        max_new_entries_per_cycle=_positive_int(
            row["max_new_entries_per_cycle"],
            label="long.max_new_entries_per_cycle",
        ),
    )


def _parse_continuous(value: object) -> ContinuousOperationalSettings:
    fields = {
        "max_active",
        "max_new_entries_per_cycle",
        "btc_trend_gate",
        "entry_leverage",
        "notional_multiplier",
        "per_position_notional_pct_equity",
    }
    row = _object(value, label="operational profile continuous", fields=fields)
    gate = row["btc_trend_gate"]
    if gate not in {"off", "uptrend", "downtrend"}:
        raise ValueError("continuous.btc_trend_gate must be off, uptrend, or downtrend")
    max_active = _positive_int(row["max_active"], label="continuous.max_active")
    max_new = _positive_int(
        row["max_new_entries_per_cycle"],
        label="continuous.max_new_entries_per_cycle",
    )
    if max_new > max_active:
        raise ValueError("continuous.max_new_entries_per_cycle cannot exceed max_active")
    return ContinuousOperationalSettings(
        max_active=max_active,
        max_new_entries_per_cycle=max_new,
        btc_trend_gate=str(gate),
        entry_leverage=_positive_float(
            row["entry_leverage"], label="continuous.entry_leverage"
        ),
        notional_multiplier=_positive_float(
            row["notional_multiplier"], label="continuous.notional_multiplier"
        ),
        per_position_notional_pct_equity=_positive_float(
            row["per_position_notional_pct_equity"],
            label="continuous.per_position_notional_pct_equity",
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
    stop_fraction = _positive_float(
        row["declared_stop_loss_fraction"], label="carry.declared_stop_loss_fraction"
    )
    if not (0.0 < stop_fraction < 1.0):
        raise ValueError("carry.declared_stop_loss_fraction must sit in (0, 1)")
    return CarryOperationalSettings(
        notional_multiplier=_positive_float(
            row["notional_multiplier"], label="carry.notional_multiplier"
        ),
        entry_leverage=_positive_float(
            row["entry_leverage"], label="carry.entry_leverage"
        ),
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
    return HedgeOperationalSettings(
        entry_leverage=_positive_float(
            row["entry_leverage"], label="hedge.entry_leverage"
        )
    )


def _validate_profile_envelopes(profile: OperationalProfile) -> None:
    """Reject producer settings that the paired account policy cannot accept."""

    risk = profile.account_risk
    leverage_requests = {
        "long": profile.long.entry_leverage,
        "continuous": profile.continuous.entry_leverage,
        "carry": profile.carry.entry_leverage,
        "hedge": profile.hedge.entry_leverage,
    }
    excessive = sorted(
        name for name, leverage in leverage_requests.items() if leverage > risk.max_leverage
    )
    if excessive:
        raise ValueError(
            "producer leverage exceeds account_risk.max_leverage: "
            + ", ".join(excessive)
        )
    if risk.max_account_gross_notional_usdt > profile.capital_reference_usdt * risk.max_leverage:
        raise ValueError(
            "account_risk account gross cap exceeds capital_reference_usdt * max_leverage"
        )
    if risk.max_initial_margin_usdt > profile.capital_reference_usdt:
        raise ValueError(
            "account_risk initial-margin cap exceeds capital_reference_usdt"
        )

    # Import lazily so the shared account-policy loader stays free of strategy
    # import cycles. These are the actual active-profile sizing constants, not
    # parallel magic numbers in the config validator.
    from .continuous_demo import (  # noqa: PLC0415
        ContinuousDemoCycleConfig,
        apply_continuous_demo_profile,
    )
    from .long_native import long_v11a_profile  # noqa: PLC0415
    from .long_native_event_demo import (  # noqa: PLC0415
        LongNativeDemoCycleConfig,
        projected_long_initial_margin_pct_equity,
    )

    long_config = LongNativeDemoCycleConfig(
        notional_multiplier=profile.long.notional_multiplier,
        entry_leverage=profile.long.entry_leverage,
        max_projected_initial_margin_pct_equity=(
            profile.long.max_projected_initial_margin_pct_equity
        ),
        max_order_notional_pct_equity=profile.long.max_order_notional_pct_equity,
        max_new_entries_per_cycle=profile.long.max_new_entries_per_cycle,
    )
    long_projection = projected_long_initial_margin_pct_equity(
        long_config, long_v11a_profile()
    )
    long_single = (
        profile.capital_reference_usdt
        * long_projection["worst_case_order_notional_pct_equity"]
    )
    long_gross = long_single * long_v11a_profile().max_concurrent_positions
    long_margin = long_gross / profile.long.entry_leverage
    if (
        long_projection["full_book_initial_margin_pct_equity"]
        > profile.long.max_projected_initial_margin_pct_equity + 1e-12
    ):
        raise ValueError(
            "long full-book margin projection exceeds its configured equity cap"
        )

    continuous_config = apply_continuous_demo_profile(
        ContinuousDemoCycleConfig(
            max_active=profile.continuous.max_active,
            max_new_entries_per_cycle=profile.continuous.max_new_entries_per_cycle,
            btc_trend_gate=profile.continuous.btc_trend_gate,
            entry_leverage=profile.continuous.entry_leverage,
            notional_multiplier=profile.continuous.notional_multiplier,
            per_position_notional_pct_equity=(
                profile.continuous.per_position_notional_pct_equity
            ),
        )
    )
    component_weights = [float(row[4]) for row in continuous_config.ensemble_components]
    vol_clamp = (
        float(continuous_config.vol_weight_clamp)
        if continuous_config.sizing_mode == "inverse_vol"
        else 1.0
    )
    base_fraction = (
        profile.continuous.per_position_notional_pct_equity
        * profile.continuous.notional_multiplier
        / 100.0
    )
    continuous_single_symbol = (
        profile.capital_reference_usdt
        * base_fraction
        * sum(component_weights)
        * vol_clamp
    )
    continuous_gross = (
        profile.capital_reference_usdt
        * base_fraction
        * max(component_weights)
        * vol_clamp
        * profile.continuous.max_active
    )
    continuous_margin = continuous_gross / profile.continuous.entry_leverage

    # CARRY worst case from the registered rule constants, not parallel magic
    # numbers: per-name cap and gross cap come from the immutable Lane-2
    # registration the producer itself loads.
    from .carry_demo import load_carry_config  # noqa: PLC0415

    carry_rule = load_carry_config()
    carry_single = (
        profile.capital_reference_usdt
        * float(carry_rule.per_name_cap)
        * profile.carry.notional_multiplier
    )
    carry_gross = (
        profile.capital_reference_usdt
        * float(carry_rule.gross_cap)
        * profile.carry.notional_multiplier
    )
    carry_margin = carry_gross / profile.carry.entry_leverage

    tolerance = max(1e-9, profile.capital_reference_usdt * 1e-12)
    if (
        max(long_single, continuous_single_symbol, carry_single)
        > risk.max_symbol_notional_usdt + tolerance
    ):
        raise ValueError("producer symbol envelope exceeds account_risk symbol cap")
    combined_gross = long_gross + continuous_gross + carry_gross
    if combined_gross > risk.max_component_gross_notional_usdt + tolerance:
        raise ValueError("combined producer envelope exceeds account_risk component cap")
    if combined_gross > risk.max_account_gross_notional_usdt + tolerance:
        raise ValueError("combined producer envelope exceeds account_risk account cap")
    if long_margin + continuous_margin + carry_margin > risk.max_initial_margin_usdt + tolerance:
        raise ValueError("combined producer margin envelope exceeds account_risk margin cap")


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
        "continuous",
        "carry",
        "hedge",
    }
    row = _object(payload, label="operational profile", fields=fields)
    if row["schema_version"] != OPERATIONAL_PROFILE_SCHEMA_VERSION:
        raise ValueError("operational profile schema_version is unsupported")
    if row["kind"] != OPERATIONAL_PROFILE_KIND:
        raise ValueError("operational profile kind is invalid")
    profile = OperationalProfile(
        capital_reference_usdt=_positive_float(
            row["capital_reference_usdt"], label="capital_reference_usdt"
        ),
        account_risk=_parse_account_risk(row["account_risk"]),
        long=_parse_long(row["long"]),
        continuous=_parse_continuous(row["continuous"]),
        carry=_parse_carry(row["carry"]),
        hedge=_parse_hedge(row["hedge"]),
        source_sha256=hashlib.sha256(data).hexdigest(),
        source_path=source_path,
    )
    _validate_profile_envelopes(profile)
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
