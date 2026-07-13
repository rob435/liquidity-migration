"""Canonical component-target identity shared by every strategy environment."""

from __future__ import annotations

from typing import Any, Mapping

from .account_intent_client import (
    ENTRY_ATTEMPT_METADATA_KEY,
    component_target_key,
    entry_attempt_key,
    requested_target,
)
from .account_service import RequestedIntent, SleeveAdapterKind


def component_target_intent(
    *,
    adapter_kind: SleeveAdapterKind,
    action: str,
    decision_ts_ms: int,
    strategy_id: str,
    component_id: str,
    symbol: str,
    signed_notional_usdt: float,
    leverage: float,
    reason: str,
    metadata: Mapping[str, Any] | None = None,
) -> RequestedIntent:
    """Build one replacement target with the repository's sole key grammar.

    Signal selection and sizing remain strategy responsibilities. This helper
    makes their resulting component identity identical in historical, paper,
    and demo for identical inputs.
    """

    sleeve = adapter_kind.value
    normalized_action = str(action).strip().lower()
    if normalized_action not in {"entry", "exit", "resize", "protect", "reconcile"}:
        raise ValueError(f"unsupported component target action: {action!r}")
    if decision_ts_ms <= 0:
        raise ValueError("component target decision_ts_ms must be positive")
    normalized_strategy = str(strategy_id).strip()
    normalized_component = str(component_id).strip()
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_strategy or not normalized_component or not normalized_symbol:
        raise ValueError("component target strategy, component, and symbol are required")
    target_key = component_target_key(
        sleeve=adapter_kind,
        strategy_id=normalized_strategy,
        component_id=normalized_component,
        symbol=normalized_symbol,
    )
    normalized_metadata = dict(metadata or {})
    if normalized_action == "entry":
        if (
            "signal_ts_ms" not in normalized_metadata
            or "signal_valid_until_ms" not in normalized_metadata
        ):
            raise ValueError(
                "entry intents require signal_ts_ms and signal_valid_until_ms"
            )
        try:
            signal_ts_ms = int(normalized_metadata["signal_ts_ms"])
            signal_valid_until_ms = int(normalized_metadata["signal_valid_until_ms"])
        except (TypeError, ValueError) as exc:
            raise ValueError("entry signal timestamps must be integers") from exc
        if signal_ts_ms < 0 or signal_valid_until_ms <= signal_ts_ms:
            raise ValueError(
                "entry intents require non-negative signal_ts_ms and later "
                "signal_valid_until_ms"
            )
        expected_attempt_key = entry_attempt_key(target_key)
        supplied_attempt_key = str(
            normalized_metadata.get(ENTRY_ATTEMPT_METADATA_KEY) or ""
        )
        if supplied_attempt_key and supplied_attempt_key != expected_attempt_key:
            raise ValueError("entry_attempt_key does not match target_key")
        normalized_metadata[ENTRY_ATTEMPT_METADATA_KEY] = expected_attempt_key
        normalized_metadata["signal_ts_ms"] = signal_ts_ms
        normalized_metadata["signal_valid_until_ms"] = signal_valid_until_ms

    return requested_target(
        adapter_kind=adapter_kind,
        decision_key=(
            f"{sleeve}-target/{normalized_strategy}/{int(decision_ts_ms)}/"
            f"{normalized_action}/{normalized_component}"
        ),
        target_key=target_key,
        strategy_id=normalized_strategy,
        component_id=normalized_component,
        symbol=normalized_symbol,
        signed_notional_usdt=signed_notional_usdt,
        leverage=leverage,
        reason=reason,
        metadata=normalized_metadata,
    )
