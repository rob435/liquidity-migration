"""Empirically verify small-order rules against Bybit's demo order endpoint."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Mapping

from .account_kernel import InstrumentRules
from .account_service_bybit import instrument_rules_from_bybit_row


def _decimal(value: object) -> Decimal:
    output = Decimal(str(value))
    if not output.is_finite() or output <= 0:
        raise RuntimeError(f"expected positive finite decimal, got {value!r}")
    return output


def _text(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _ceil_steps(qty: Decimal, step: Decimal) -> int:
    return int((qty / step).to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True, slots=True)
class DemoRuleProbeAttempt:
    step_count: int
    qty: float
    notional_usdt: float
    accepted: bool
    rejection: str = ""


@dataclass(frozen=True, slots=True)
class DemoRuleProbeEvidence:
    symbol: str
    probe_price: float
    lowest_accepted_qty: float
    lowest_accepted_notional_usdt: float
    highest_rejected_qty: float
    highest_rejected_notional_usdt: float
    tested_leverage: float
    attempts: tuple[DemoRuleProbeAttempt, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_demo_instrument_rule(
    client: Any,
    *,
    instrument_row: Mapping[str, Any],
    ticker_row: Mapping[str, Any],
    observed_ts_ns: int,
    max_probe_notional_usdt: float,
    leverage: float = 10.0,
    link_namespace: str = "demo-rule",
) -> tuple[InstrumentRules, DemoRuleProbeEvidence]:
    """Find the smallest demo-accepted PostOnly notional for one symbol.

    Structural tick/quantity/leverage values are read through ``api-demo``.
    The notional threshold is then tested on the actual order-create endpoint.
    Accepted PostOnly orders are cancelled immediately.  Only documented
    110094 lower-notional rejects are treated as search observations; transport,
    margin, permission, price-band, and other failures abort the probe.
    """

    structural = instrument_rules_from_bybit_row(
        instrument_row,
        source="bybit_api_demo_instruments_info",
        environment="demo",
        observed_ts_ns=observed_ts_ns,
    )
    symbol = structural.symbol
    step = _decimal(structural.qty_step)
    min_qty = _decimal(structural.min_qty)
    tick = _decimal(structural.tick_size)
    bid = _decimal(ticker_row.get("bid1Price") or ticker_row.get("lastPrice"))
    # One tick below bid plus PostOnly is non-marketable at submission.  A
    # later fill is still possible, so the CLI performs a final flatness audit.
    probe_price = ((bid - tick) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    if probe_price <= 0:
        raise RuntimeError(f"{symbol}: cannot construct a positive PostOnly probe price")
    max_notional = _decimal(max_probe_notional_usdt)
    min_steps = _ceil_steps(min_qty, step)
    max_steps = int((max_notional / (step * probe_price)).to_integral_value(rounding=ROUND_FLOOR))
    if max_steps < min_steps:
        raise RuntimeError(
            f"{symbol}: max probe notional {max_notional} is below structural min qty notional"
        )
    tested_leverage = min(float(leverage), structural.max_leverage or float(leverage))
    client.set_leverage(
        symbol=symbol,
        buy_leverage=tested_leverage,
        sell_leverage=tested_leverage,
    )

    attempts: list[DemoRuleProbeAttempt] = []
    attempt_number = 0

    def accepted(step_count: int) -> bool:
        nonlocal attempt_number
        attempt_number += 1
        qty = step * step_count
        notional = qty * probe_price
        link = f"lm-{link_namespace[:10]}-{symbol[:8]}-{observed_ts_ns % 1_000_000_000:x}-{attempt_number}"
        try:
            client.place_order(
                symbol=symbol,
                side="Buy",
                orderType="Limit",
                qty=_text(qty),
                price=_text(probe_price),
                timeInForce="PostOnly",
                reduceOnly=False,
                orderLinkId=link[:36],
            )
        except Exception as exc:  # noqa: BLE001 - classify the venue receipt below
            message = str(exc)
            if "110094" not in message and "notional value below" not in message.lower():
                raise RuntimeError(f"{symbol}: non-threshold probe failure: {message}") from exc
            attempts.append(DemoRuleProbeAttempt(
                step_count=step_count,
                qty=float(qty),
                notional_usdt=float(notional),
                accepted=False,
                rejection=message[:500],
            ))
            return False
        attempts.append(DemoRuleProbeAttempt(
            step_count=step_count,
            qty=float(qty),
            notional_usdt=float(notional),
            accepted=True,
        ))
        last_cancel_error: Exception | None = None
        for _ in range(10):
            try:
                client.cancel_order(symbol=symbol, order_link_id=link[:36])
                return True
            except Exception as exc:  # noqa: BLE001 - async ack/cancel race
                last_cancel_error = exc
                time.sleep(0.05)
        raise RuntimeError(
            f"{symbol}: accepted probe order could not be cancelled: {last_cancel_error}"
        )

    if accepted(min_steps):
        lowest_accepted = min_steps
        highest_rejected = 0
    else:
        highest_rejected = min_steps
        candidate = min_steps
        while candidate < max_steps:
            candidate = min(candidate * 2, max_steps)
            if accepted(candidate):
                break
            highest_rejected = candidate
        else:
            raise RuntimeError(
                f"{symbol}: no accepted order at or below ${max_probe_notional_usdt:g}"
            )
        if not attempts[-1].accepted:
            raise RuntimeError(
                f"{symbol}: no accepted order at or below ${max_probe_notional_usdt:g}"
            )
        lowest_accepted = candidate
        low, high = highest_rejected + 1, lowest_accepted
        while low < high:
            mid = (low + high) // 2
            if accepted(mid):
                high = mid
            else:
                highest_rejected = mid
                low = mid + 1
        lowest_accepted = low

    accepted_qty = step * lowest_accepted
    accepted_notional = accepted_qty * probe_price
    rejected_qty = step * highest_rejected
    rejected_notional = rejected_qty * probe_price
    # This is a conservative, venue-observed upper bound on the effective
    # notional floor at the probe price, at most one qty step above the exact
    # hidden threshold.  It replaces the old arbitrary $25 resize floor.
    rule = InstrumentRules(
        symbol=symbol,
        qty_step=structural.qty_step,
        min_qty=structural.min_qty,
        min_notional=float(accepted_notional),
        tick_size=structural.tick_size,
        max_order_qty=structural.max_order_qty,
        max_leverage=structural.max_leverage,
        source="bybit_demo_post_only_acceptance_probe",
        environment="demo",
        observed_ts_ns=observed_ts_ns,
    )
    evidence = DemoRuleProbeEvidence(
        symbol=symbol,
        probe_price=float(probe_price),
        lowest_accepted_qty=float(accepted_qty),
        lowest_accepted_notional_usdt=float(accepted_notional),
        highest_rejected_qty=float(rejected_qty),
        highest_rejected_notional_usdt=float(rejected_notional),
        tested_leverage=tested_leverage,
        attempts=tuple(attempts),
    )
    if not math.isfinite(rule.min_notional) or rule.min_notional <= 0:
        raise RuntimeError(f"{symbol}: invalid observed minimum notional")
    return rule, evidence
