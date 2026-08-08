"""Single-source operational sizing and account-risk configuration.

The profile is shared by every target producer and the account owner.  It is
strictly operational: changing it does not promote research evidence, and the
account owner remains the final authority for every risk-increasing target.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from liquidity_migration.account.account_contracts import AccountRiskPolicy, SleeveCapitalLimit
from liquidity_migration.core.artifact_snapshot import StableFileSnapshot, read_stable_file


OPERATIONAL_PROFILE_SCHEMA_VERSION = 1
OPERATIONAL_PROFILE_KIND = "liquidity_migration_operational_profile"

#: The sleeves a partition may name. Closed, so a typo cannot create a partition
#: nothing spends while the real sleeve stays unlisted and refused.
PARTITIONABLE_SLEEVES: tuple[str, ...] = ("carry", "continuous", "hedge", "long")


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
class SleeveLimitSettings:
    """One sleeve's declared share of the account envelope."""

    sleeve: str
    max_gross_notional_usdt: float
    max_initial_margin_usdt: float


@dataclass(frozen=True, slots=True)
class AccountRiskSettings:
    max_component_gross_notional_usdt: float
    max_account_gross_notional_usdt: float
    max_symbol_notional_usdt: float
    max_initial_margin_usdt: float
    max_leverage: float
    quantity_tolerance: float
    max_daily_loss_usdt: float = 0.0
    sleeve_limits: tuple[SleeveLimitSettings, ...] = ()

    def sleeve_limit(self, sleeve: str) -> SleeveLimitSettings | None:
        for limit in self.sleeve_limits:
            if limit.sleeve == sleeve:
                return limit
        return None

    def to_policy(self) -> AccountRiskPolicy:
        return AccountRiskPolicy(
            max_component_gross_notional_usdt=self.max_component_gross_notional_usdt,
            max_account_gross_notional_usdt=self.max_account_gross_notional_usdt,
            max_symbol_notional_usdt=self.max_symbol_notional_usdt,
            max_initial_margin_usdt=self.max_initial_margin_usdt,
            max_leverage=self.max_leverage,
            quantity_tolerance=self.quantity_tolerance,
            max_daily_loss_usdt=self.max_daily_loss_usdt,
            sleeve_limits=tuple(
                SleeveCapitalLimit(
                    sleeve=limit.sleeve,
                    max_gross_notional_usdt=limit.max_gross_notional_usdt,
                    max_initial_margin_usdt=limit.max_initial_margin_usdt,
                )
                for limit in self.sleeve_limits
            ),
        )


@dataclass(frozen=True, slots=True)
class LongOperationalSettings:
    notional_multiplier: float
    entry_leverage: float
    max_projected_initial_margin_pct_equity: float
    order_notional_pct_equity: float
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

    Rule parameters (entry/exit prints, filters, per-name and gross caps) come
    from the registered config the producer loads. This block carries only the
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
    continuous: ContinuousOperationalSettings
    carry: CarryOperationalSettings
    hedge: HedgeOperationalSettings
    source_sha256: str
    source_path: Path | None = None
    schema_version: int = OPERATIONAL_PROFILE_SCHEMA_VERSION
    kind: str = OPERATIONAL_PROFILE_KIND
    capital_reference: CapitalReferenceSettings = CapitalReferenceSettings()


def _parse_sleeve_limits(value: object, *, risk: AccountRiskSettings) -> tuple[SleeveLimitSettings, ...]:
    """Parse and prove a genuine partition of the account envelope.

    The shares must sum inside the account caps; overlapping shares would still
    let one sleeve crowd another out at the account limit.
    """

    if not isinstance(value, Mapping):
        raise ValueError("operational profile sleeve_limits must be a JSON object")
    unknown = sorted(set(value) - set(PARTITIONABLE_SLEEVES))
    if unknown:
        raise ValueError(
            "operational profile sleeve_limits names unknown sleeves: " + ", ".join(unknown)
        )
    if not value:
        # An empty object reads as "partitioned" but behaves as "everything
        # refused".
        raise ValueError("operational profile sleeve_limits must name at least one sleeve")
    limits: list[SleeveLimitSettings] = []
    for sleeve in PARTITIONABLE_SLEEVES:
        if sleeve not in value:
            continue
        row = _object(
            value[sleeve],
            label=f"operational profile sleeve_limits.{sleeve}",
            fields={"max_gross_notional_usdt", "max_initial_margin_usdt"},
        )
        limits.append(
            SleeveLimitSettings(
                sleeve=sleeve,
                max_gross_notional_usdt=_positive_float(
                    row["max_gross_notional_usdt"],
                    label=f"sleeve_limits.{sleeve}.max_gross_notional_usdt",
                ),
                max_initial_margin_usdt=_positive_float(
                    row["max_initial_margin_usdt"],
                    label=f"sleeve_limits.{sleeve}.max_initial_margin_usdt",
                ),
            )
        )
    tolerance = max(1e-9, risk.max_account_gross_notional_usdt * 1e-12)
    total_gross = math.fsum(limit.max_gross_notional_usdt for limit in limits)
    total_margin = math.fsum(limit.max_initial_margin_usdt for limit in limits)
    if total_gross > risk.max_account_gross_notional_usdt + tolerance:
        raise ValueError(
            "sleeve_limits gross shares sum above account_risk.max_account_gross_notional_usdt"
        )
    if total_margin > risk.max_initial_margin_usdt + tolerance:
        raise ValueError(
            "sleeve_limits margin shares sum above account_risk.max_initial_margin_usdt"
        )
    return tuple(limits)


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
        # Absent ``max_daily_loss_usdt`` means no daily loss ceiling; absent
        # ``sleeve_limits`` means one shared, unpartitioned envelope.
        optional=frozenset({"max_daily_loss_usdt", "sleeve_limits"}),
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
    if row.get("sleeve_limits") is not None:
        settings = replace(
            settings,
            sleeve_limits=_parse_sleeve_limits(row["sleeve_limits"], risk=settings),
        )
    return settings


def _parse_long(value: object) -> LongOperationalSettings:
    fields = {
        "notional_multiplier",
        "entry_leverage",
        "max_projected_initial_margin_pct_equity",
        "order_notional_pct_equity",
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
        row["order_notional_pct_equity"],
        label="long.order_notional_pct_equity",
        allow_zero=True,
    )
    if order_cap > 10.0:
        raise ValueError("long.order_notional_pct_equity cannot exceed 10")
    return LongOperationalSettings(
        notional_multiplier=_positive_float(
            row["notional_multiplier"], label="long.notional_multiplier"
        ),
        entry_leverage=_positive_float(
            row["entry_leverage"], label="long.entry_leverage"
        ),
        max_projected_initial_margin_pct_equity=margin_fraction,
        order_notional_pct_equity=order_cap,
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
        btc_trend_gate=gate,
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
    # Dials may pin a cap exactly at its bound (gross cap = reference * max
    # leverage, margin cap = reference), and every equity rebase re-derives the
    # absolutes by multiplication, so each comparison carries a rounding
    # allowance far below economic size.
    tolerance = max(1e-9, profile.capital_reference_usdt * 1e-12)
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
    if (
        risk.max_account_gross_notional_usdt
        > profile.capital_reference_usdt * risk.max_leverage + tolerance
    ):
        raise ValueError(
            "account_risk account gross cap exceeds capital_reference_usdt * max_leverage"
        )
    if risk.max_initial_margin_usdt > profile.capital_reference_usdt + tolerance:
        raise ValueError(
            "account_risk initial-margin cap exceeds capital_reference_usdt"
        )

    # Imported lazily to keep the shared account-policy loader out of strategy
    # import cycles; these are the real sizing constants, not copies of them.
    from liquidity_migration.strategy.continuous_demo import (  # noqa: PLC0415
        ContinuousDemoCycleConfig,
        apply_continuous_demo_profile,
    )
    from liquidity_migration.research.backtest.long_native import long_v11a_profile  # noqa: PLC0415
    from liquidity_migration.strategy.long_native_event_demo import (  # noqa: PLC0415
        LongNativeDemoCycleConfig,
        projected_long_initial_margin_pct_equity,
    )

    long_config = LongNativeDemoCycleConfig(
        notional_multiplier=profile.long.notional_multiplier,
        entry_leverage=profile.long.entry_leverage,
        max_projected_initial_margin_pct_equity=(
            profile.long.max_projected_initial_margin_pct_equity
        ),
        order_notional_pct_equity=profile.long.order_notional_pct_equity,
        max_new_entries_per_cycle=profile.long.max_new_entries_per_cycle,
    )
    long_strategy = long_v11a_profile()
    long_projection = projected_long_initial_margin_pct_equity(long_config, long_strategy)
    long_single = (
        profile.capital_reference_usdt
        * long_projection["worst_case_order_notional_pct_equity"]
    )
    long_gross = long_single * long_strategy.max_concurrent_positions
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

    # CARRY worst case from the registered rule constants the producer loads.
    from liquidity_migration.strategy.carry_demo import load_carry_config  # noqa: PLC0415

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

    # When partitioned, each producer must fit its own share, not just the total.
    if risk.sleeve_limits:
        producer_envelopes = {
            "long": (long_gross, long_margin),
            "continuous": (continuous_gross, continuous_margin),
            "carry": (carry_gross, carry_margin),
        }
        for sleeve, (sleeve_gross, sleeve_margin) in sorted(producer_envelopes.items()):
            limit = risk.sleeve_limit(sleeve)
            if limit is None:
                # An unpartitioned sleeve is refused at runtime, so a nonzero
                # envelope for it is a config that can only produce rejections.
                if sleeve_gross > tolerance:
                    raise ValueError(
                        f"producer sleeve {sleeve!r} has an envelope but no sleeve_limits share; "
                        "give it a share or shrink it to zero"
                    )
                continue
            if sleeve_gross > limit.max_gross_notional_usdt + tolerance:
                raise ValueError(
                    f"producer sleeve {sleeve!r} gross envelope exceeds its sleeve_limits share"
                )
            if sleeve_margin > limit.max_initial_margin_usdt + tolerance:
                raise ValueError(
                    f"producer sleeve {sleeve!r} margin envelope exceeds its sleeve_limits share"
                )


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
            "capital_reference.mode must be "
            f"{CAPITAL_REFERENCE_FIXED!r} or {CAPITAL_REFERENCE_ACCOUNT_EQUITY!r}"
        )
    equity_fraction = _positive_float(
        row.get("equity_fraction", 1.0), label="capital_reference.equity_fraction"
    )
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


def profile_at_capital_reference(
    profile: OperationalProfile,
    reference_usdt: float,
) -> OperationalProfile:
    """Rescale every absolute limit to a new capital reference, and re-prove it.

    Every envelope and cap is linear in the reference, so this is a pure rescale
    of ratios the profile already encodes. ``max_leverage`` and
    ``quantity_tolerance`` are scale-free and untouched — which is why the
    envelope proof is re-run rather than argued from linearity.
    """

    target = float(reference_usdt)
    if not math.isfinite(target) or target <= 0.0:
        raise ValueError("capital reference must be finite and positive")
    scale = target / profile.capital_reference_usdt
    risk = profile.account_risk
    rescaled = replace(
        profile,
        capital_reference_usdt=target,
        account_risk=AccountRiskSettings(
            max_component_gross_notional_usdt=risk.max_component_gross_notional_usdt * scale,
            max_account_gross_notional_usdt=risk.max_account_gross_notional_usdt * scale,
            max_symbol_notional_usdt=risk.max_symbol_notional_usdt * scale,
            max_initial_margin_usdt=risk.max_initial_margin_usdt * scale,
            max_leverage=risk.max_leverage,
            quantity_tolerance=risk.quantity_tolerance,
            max_daily_loss_usdt=risk.max_daily_loss_usdt * scale,
            # Shares are ratios of the reference like the caps they partition,
            # so they rescale with them.
            sleeve_limits=tuple(
                SleeveLimitSettings(
                    sleeve=limit.sleeve,
                    max_gross_notional_usdt=limit.max_gross_notional_usdt * scale,
                    max_initial_margin_usdt=limit.max_initial_margin_usdt * scale,
                )
                for limit in risk.sleeve_limits
            ),
        ),
    )
    _validate_profile_envelopes(rescaled)
    return rescaled


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
