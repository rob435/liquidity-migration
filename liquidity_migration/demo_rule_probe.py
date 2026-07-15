"""Empirically verify small-order rules against Bybit's demo order endpoint."""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Mapping

from .account_kernel import InstrumentRules
from .account_service_bybit import instrument_rules_from_bybit_row


DEMO_RULES_SCHEMA_VERSION = 3
DEMO_RULES_KIND = "bybit_demo_instrument_rules"
DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION = 1
DEMO_RULE_PROBE_EVIDENCE_KIND = "bybit_demo_instrument_rule_probe"
DEMO_RULE_PROBE_FAILURE_SCHEMA_VERSION = 1
DEMO_RULE_PROBE_FAILURE_KIND = "bybit_demo_instrument_rule_probe_attempt"
REGISTERED_MAX_PROBE_NOTIONAL_USDT = 200.0
REGISTERED_PROBE_DISTANCE_BPS = 100.0
REGISTERED_MAX_PRIVATE_REQUESTS_PER_SECOND = 5
REGISTERED_MAX_TESTED_LEVERAGE = 10.0
TERMINAL_HISTORY_TIMEOUT_SECONDS = 30.0
TERMINAL_HISTORY_MAX_POLLS = 100

ORDER_CREATE_SOURCE = "bybit_api_demo_order_create"
ORDER_CANCEL_SOURCE = "bybit_api_demo_order_cancel"
ORDER_HISTORY_SOURCE = "bybit_api_demo_order_history"
TRADE_HISTORY_SOURCE = "bybit_api_demo_trade_history"

_PENDING_ORDER_STATUSES = {"created", "new", "untriggered"}


def require_registered_demo_rule_probe_parameters(
    *,
    max_probe_notional_usdt: object,
    probe_distance_bps: object,
    max_private_requests_per_second: object,
) -> tuple[float, float, int]:
    try:
        max_notional = float(str(max_probe_notional_usdt))
        distance = float(str(probe_distance_bps))
    except (TypeError, ValueError) as exc:
        raise ValueError("demo-rule probe parameters must be numeric") from exc
    if type(max_private_requests_per_second) is not int:
        raise ValueError("demo-rule private request ceiling must be an integer")
    rate = max_private_requests_per_second
    if (
        not math.isfinite(max_notional)
        or max_notional <= 0.0
        or max_notional > REGISTERED_MAX_PROBE_NOTIONAL_USDT
    ):
        raise ValueError("demo-rule max probe notional cannot exceed registered 200 USDT")
    if not math.isfinite(distance) or distance != REGISTERED_PROBE_DISTANCE_BPS:
        raise ValueError("demo-rule probe distance is fixed prospectively at 100 bps")
    if rate <= 0 or rate > REGISTERED_MAX_PRIVATE_REQUESTS_PER_SECOND:
        raise ValueError("demo-rule private request ceiling cannot exceed registered 5/s")
    return max_notional, distance, rate


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
    outcome: str
    rejection: str = ""
    order_link_id: str = ""
    order_id: str = ""
    create_ack_source: str = ""
    create_ack_order_id: str = ""
    create_ack_order_link_id: str = ""
    cancel_ack_source: str = ""
    cancel_ack_order_id: str = ""
    cancel_ack_order_link_id: str = ""
    order_history_source: str = ""
    order_history_query_symbol: str = ""
    order_history_query_order_id: str = ""
    order_history_query_order_link_id: str = ""
    terminal_order_id: str = ""
    terminal_order_link_id: str = ""
    terminal_status: str = ""
    terminal_cum_exec_qty: str = ""
    terminal_cum_exec_value: str = ""
    terminal_observed_ts_ns: int = 0
    terminal_poll_count: int = 0
    terminal_confirmation_polls: int = 0
    trade_history_source: str = ""
    trade_history_query_symbol: str = ""
    trade_history_query_order_id: str = ""
    trade_history_query_order_link_id: str = ""
    trade_history_row_count: int = -1


@dataclass(frozen=True, slots=True)
class DemoRuleProbeEvidence:
    schema_version: int
    kind: str
    environment: str
    observed_ts_ns: int
    symbol: str
    probe_price: float
    probe_distance_bps: float
    lowest_accepted_qty: float
    lowest_accepted_notional_usdt: float
    highest_rejected_qty: float
    highest_rejected_notional_usdt: float
    tested_leverage: float
    terminal_history_timeout_seconds: float
    terminal_history_poll_seconds: float
    terminal_history_max_polls: int
    required_terminal_confirmation_polls: int
    attempts: tuple[DemoRuleProbeAttempt, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _ProbeIdentityError(RuntimeError):
    """The venue response did not bind to the exact probe order identity."""


def _required_identity(
    row: Mapping[str, Any] | object,
    *,
    symbol: str,
    order_id: str,
    order_link_id: str,
    label: str,
) -> None:
    if not isinstance(row, Mapping):
        raise _ProbeIdentityError(f"{label} must be an object")
    observed_symbol = str(row.get("symbol") or symbol)
    observed_order_id = str(row.get("orderId") or "")
    observed_order_link_id = str(row.get("orderLinkId") or "")
    if (
        observed_symbol != symbol
        or observed_order_id != order_id
        or observed_order_link_id != order_link_id
    ):
        raise _ProbeIdentityError(
            f"{label} identity mismatch: expected symbol={symbol!r} orderId={order_id!r} "
            f"orderLinkId={order_link_id!r}, observed symbol={observed_symbol!r} "
            f"orderId={observed_order_id!r} orderLinkId={observed_order_link_id!r}"
        )


def _ack_identity(
    row: Mapping[str, Any] | object,
    *,
    symbol: str,
    order_link_id: str,
    label: str,
) -> str:
    if not isinstance(row, Mapping):
        raise _ProbeIdentityError(f"{label} must be an object")
    order_id = str(row.get("orderId") or "")
    observed_order_link_id = str(row.get("orderLinkId") or "")
    if not order_id or observed_order_link_id != order_link_id:
        raise _ProbeIdentityError(
            f"{label} identity mismatch: expected non-empty orderId and "
            f"orderLinkId={order_link_id!r}, observed orderId={order_id!r} "
            f"orderLinkId={observed_order_link_id!r}"
        )
    observed_symbol = str(row.get("symbol") or symbol)
    if observed_symbol != symbol:
        raise _ProbeIdentityError(
            f"{label} symbol mismatch: expected {symbol!r}, observed {observed_symbol!r}"
        )
    return order_id


def _decimal_text(value: object, *, label: str) -> str:
    try:
        output = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001 - normalize malformed venue evidence
        raise RuntimeError(f"{label} is not a decimal: {value!r}") from exc
    if not output.is_finite() or output < 0:
        raise RuntimeError(f"{label} must be a non-negative finite decimal")
    return _text(output) if output else "0"


def _require_zero_decimal(value: object, *, label: str) -> str:
    output = _decimal_text(value, label=label)
    if Decimal(output) != 0:
        raise RuntimeError(f"{label} proves a probe fill: {output}")
    return output


def _positive_finite_float(value: object, *, label: str) -> float:
    try:
        output = float(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive finite number") from exc
    if not math.isfinite(output) or output <= 0.0:
        raise ValueError(f"{label} must be a positive finite number")
    return output


def validate_demo_rule_probe_evidence(
    payload: Mapping[str, Any],
    *,
    expected_symbol: str,
    expected_observed_ts_ns: int,
    expected_rules: InstrumentRules,
    max_probe_notional_usdt: float,
) -> float:
    """Validate one symbol's terminal, identity-bound no-fill probe evidence.

    Returns the verified minimum notional. A create or cancel acknowledgement is
    deliberately insufficient: every accepted row must carry exact order/link
    identity through terminal order history and an empty execution-history read.
    """

    symbol = expected_symbol.upper()
    if expected_rules.symbol.upper() != symbol:
        raise ValueError(f"demo rule {symbol} structural rule identity is invalid")
    max_probe_notional = _positive_finite_float(
        max_probe_notional_usdt,
        label=f"demo rule {symbol} max probe notional",
    )
    if max_probe_notional > REGISTERED_MAX_PROBE_NOTIONAL_USDT:
        raise ValueError(f"demo rule {symbol} max probe notional exceeds registered 200 USDT")
    qty_step = _positive_finite_float(
        expected_rules.qty_step,
        label=f"demo rule {symbol} quantity step",
    )
    structural_min_qty = _positive_finite_float(
        expected_rules.min_qty,
        label=f"demo rule {symbol} structural minimum quantity",
    )
    structural_min_notional = _positive_finite_float(
        expected_rules.min_notional,
        label=f"demo rule {symbol} structural minimum notional",
    )
    if (
        payload.get("schema_version") != DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION
        or payload.get("kind") != DEMO_RULE_PROBE_EVIDENCE_KIND
        or payload.get("environment") != "demo"
        or str(payload.get("symbol") or "").upper() != symbol
        or int(payload.get("observed_ts_ns") or 0) != expected_observed_ts_ns
    ):
        raise ValueError(f"demo rule {symbol} probe evidence identity is invalid")
    minimum_notional = _positive_finite_float(
        payload.get("lowest_accepted_notional_usdt"),
        label=f"demo rule {symbol} lowest accepted notional",
    )
    minimum_qty = _positive_finite_float(
        payload.get("lowest_accepted_qty"),
        label=f"demo rule {symbol} lowest accepted quantity",
    )
    probe_price = _positive_finite_float(
        payload.get("probe_price"),
        label=f"demo rule {symbol} probe price",
    )
    probe_distance = _positive_finite_float(
        payload.get("probe_distance_bps"),
        label=f"demo rule {symbol} probe distance",
    )
    if probe_distance != REGISTERED_PROBE_DISTANCE_BPS:
        raise ValueError(f"demo rule {symbol} probe distance differs from registered 100 bps")
    tested_leverage = _positive_finite_float(
        payload.get("tested_leverage"),
        label=f"demo rule {symbol} tested leverage",
    )
    if tested_leverage > REGISTERED_MAX_TESTED_LEVERAGE:
        raise ValueError(f"demo rule {symbol} tested leverage exceeds registered 10x")
    if expected_rules.max_leverage > 0.0 and tested_leverage > expected_rules.max_leverage:
        raise ValueError(f"demo rule {symbol} tested leverage exceeds structural maximum")
    _positive_finite_float(
        payload.get("terminal_history_timeout_seconds"),
        label=f"demo rule {symbol} terminal history timeout",
    )
    try:
        poll_seconds = float(str(payload.get("terminal_history_poll_seconds")))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"demo rule {symbol} terminal poll interval is invalid") from exc
    raw_max_polls = payload.get("terminal_history_max_polls")
    raw_required_confirmations = payload.get("required_terminal_confirmation_polls")
    if type(raw_max_polls) is not int or type(raw_required_confirmations) is not int:
        raise ValueError(f"demo rule {symbol} terminal verification counts are invalid")
    max_polls = raw_max_polls
    required_confirmations = raw_required_confirmations
    if (
        not math.isfinite(poll_seconds)
        or poll_seconds < 0.0
        or max_polls <= 0
        or required_confirmations < 2
        or required_confirmations > max_polls
    ):
        raise ValueError(f"demo rule {symbol} terminal verification bounds are invalid")
    attempts = payload.get("attempts")
    if not isinstance(attempts, (list, tuple)) or not attempts:
        raise ValueError(f"demo rule {symbol} requires non-empty probe attempts")

    accepted_rows: list[Mapping[str, Any]] = []
    rejected_rows: list[Mapping[str, Any]] = []
    for index, raw in enumerate(attempts):
        if not isinstance(raw, Mapping):
            raise ValueError(f"demo rule {symbol} attempt {index} must be an object")
        outcome = str(raw.get("outcome") or "")
        accepted = raw.get("accepted") is True
        order_link_id = str(raw.get("order_link_id") or "")
        if not order_link_id:
            raise ValueError(f"demo rule {symbol} attempt {index} lacks orderLinkId identity")
        attempt_qty = _positive_finite_float(
            raw.get("qty"), label=f"demo rule {symbol} attempt quantity"
        )
        attempt_notional = _positive_finite_float(
            raw.get("notional_usdt"),
            label=f"demo rule {symbol} attempt notional",
        )
        if attempt_notional > max_probe_notional + max(
            1e-12,
            max_probe_notional * 1e-12,
        ):
            raise ValueError(f"demo rule {symbol} attempt exceeds the receipt probe-notional cap")
        expected_notional = attempt_qty * probe_price
        if abs(attempt_notional - expected_notional) > max(1e-12, expected_notional * 1e-12):
            raise ValueError(f"demo rule {symbol} attempt notional does not match qty * probe price")
        raw_step_count = raw.get("step_count")
        if type(raw_step_count) is not int or raw_step_count <= 0:
            raise ValueError(f"demo rule {symbol} attempt {index} has invalid step count")
        expected_qty = qty_step * raw_step_count
        if abs(attempt_qty - expected_qty) > max(1e-12, expected_qty * 1e-12):
            raise ValueError(f"demo rule {symbol} attempt quantity does not match its quantity step count")
        if attempt_qty + max(1e-12, structural_min_qty * 1e-12) < structural_min_qty:
            raise ValueError(f"demo rule {symbol} attempt quantity is below structural minimum")
        if expected_rules.max_order_qty > 0.0 and attempt_qty > expected_rules.max_order_qty:
            raise ValueError(f"demo rule {symbol} attempt quantity exceeds structural maximum")
        if outcome == "threshold_rejected":
            rejection = str(raw.get("rejection") or "")
            if (
                accepted
                or not rejection
                or (
                    "110094" not in rejection
                    and "notional value below" not in rejection.lower()
                )
            ):
                raise ValueError(f"demo rule {symbol} threshold rejection is incomplete")
            if raw.get("create_ack_source") or raw.get("order_id"):
                raise ValueError(f"demo rule {symbol} rejected attempt claims a create acknowledgement")
            rejected_rows.append(raw)
            continue
        if outcome != "verified_cancelled_no_fill" or not accepted:
            raise ValueError(f"demo rule {symbol} contains an unverified accepted attempt")

        order_id = str(raw.get("order_id") or "")
        if not order_id:
            raise ValueError(f"demo rule {symbol} accepted attempt lacks orderId identity")
        expected_strings = {
            "create_ack_source": ORDER_CREATE_SOURCE,
            "create_ack_order_id": order_id,
            "create_ack_order_link_id": order_link_id,
            "cancel_ack_source": ORDER_CANCEL_SOURCE,
            "cancel_ack_order_id": order_id,
            "cancel_ack_order_link_id": order_link_id,
            "order_history_source": ORDER_HISTORY_SOURCE,
            "order_history_query_symbol": symbol,
            "order_history_query_order_id": order_id,
            "order_history_query_order_link_id": order_link_id,
            "terminal_order_id": order_id,
            "terminal_order_link_id": order_link_id,
            "terminal_status": "Cancelled",
            "trade_history_source": TRADE_HISTORY_SOURCE,
            "trade_history_query_symbol": symbol,
            "trade_history_query_order_id": order_id,
            "trade_history_query_order_link_id": order_link_id,
        }
        for field, expected in expected_strings.items():
            if str(raw.get(field) or "") != expected:
                raise ValueError(
                    f"demo rule {symbol} accepted attempt {field} does not bind the probe order"
                )
        try:
            cum_qty = Decimal(str(raw.get("terminal_cum_exec_qty")))
            cum_value = Decimal(str(raw.get("terminal_cum_exec_value")))
        except Exception as exc:  # noqa: BLE001 - validate persisted evidence
            raise ValueError(f"demo rule {symbol} accepted attempt has invalid execution totals") from exc
        if (
            not cum_qty.is_finite()
            or not cum_value.is_finite()
            or cum_qty != 0
            or cum_value != 0
        ):
            raise ValueError(f"demo rule {symbol} accepted attempt is not fill-free")
        raw_poll_count = raw.get("terminal_poll_count")
        raw_confirmations = raw.get("terminal_confirmation_polls")
        if type(raw_poll_count) is not int or type(raw_confirmations) is not int:
            raise ValueError(f"demo rule {symbol} accepted attempt poll counts are invalid")
        poll_count = raw_poll_count
        confirmations = raw_confirmations
        if (
            poll_count < required_confirmations
            or poll_count > max_polls
            or confirmations != required_confirmations
            or confirmations > poll_count
        ):
            raise ValueError(f"demo rule {symbol} accepted attempt lacks bounded terminal confirmation")
        terminal_observed_ts_ns = raw.get("terminal_observed_ts_ns")
        if (
            type(terminal_observed_ts_ns) is not int
            or terminal_observed_ts_ns < expected_observed_ts_ns
        ):
            raise ValueError(f"demo rule {symbol} terminal observation predates the probe")
        if type(raw.get("trade_history_row_count")) is not int or raw.get(
            "trade_history_row_count"
        ) != 0:
            raise ValueError(f"demo rule {symbol} accepted attempt has execution-history rows")
        accepted_rows.append(raw)

    if not accepted_rows:
        raise ValueError(f"demo rule {symbol} has no terminally verified accepted attempt")
    observed_min_notional = min(float(row["notional_usdt"]) for row in accepted_rows)
    observed_min_qty = min(float(row["qty"]) for row in accepted_rows)
    tolerance = max(1e-12, minimum_notional * 1e-12)
    if abs(observed_min_notional - minimum_notional) > tolerance:
        raise ValueError(f"demo rule {symbol} minimum does not match accepted attempts")
    if abs(observed_min_qty - minimum_qty) > max(1e-12, minimum_qty * 1e-12):
        raise ValueError(f"demo rule {symbol} minimum quantity does not match accepted attempts")
    if abs(structural_min_notional - minimum_notional) > tolerance:
        raise ValueError(f"demo rule {symbol} structural minimum does not match acceptance evidence")
    if rejected_rows:
        highest_rejected = max(float(row["notional_usdt"]) for row in rejected_rows)
        if highest_rejected >= minimum_notional:
            raise ValueError(f"demo rule {symbol} rejected notional is not below the accepted minimum")
        declared_rejected = float(payload.get("highest_rejected_notional_usdt") or 0.0)
        if abs(highest_rejected - declared_rejected) > max(1e-12, highest_rejected * 1e-12):
            raise ValueError(f"demo rule {symbol} highest rejection does not match attempts")
        highest_rejected_qty = max(float(row["qty"]) for row in rejected_rows)
        declared_rejected_qty = float(payload.get("highest_rejected_qty") or 0.0)
        if abs(highest_rejected_qty - declared_rejected_qty) > max(
            1e-12,
            highest_rejected_qty * 1e-12,
        ):
            raise ValueError(f"demo rule {symbol} highest rejected quantity does not match attempts")
    elif (
        float(payload.get("highest_rejected_notional_usdt") or 0.0) != 0.0
        or float(payload.get("highest_rejected_qty") or 0.0) != 0.0
    ):
        raise ValueError(f"demo rule {symbol} declares an unobserved threshold rejection")
    return minimum_notional


def probe_demo_instrument_rule(
    client: Any,
    *,
    instrument_row: Mapping[str, Any],
    ticker_row: Mapping[str, Any],
    observed_ts_ns: int,
    max_probe_notional_usdt: float,
    leverage: float = 10.0,
    probe_distance_bps: float = 100.0,
    link_namespace: str = "demo-rule",
    terminal_history_timeout_seconds: float = TERMINAL_HISTORY_TIMEOUT_SECONDS,
    terminal_history_poll_seconds: float = 0.1,
    terminal_history_max_polls: int = TERMINAL_HISTORY_MAX_POLLS,
    terminal_confirmation_polls: int = 2,
    attempt_sink: list[DemoRuleProbeAttempt] | None = None,
) -> tuple[InstrumentRules, DemoRuleProbeEvidence]:
    """Find the smallest demo-accepted PostOnly notional for one symbol.

    Structural tick/quantity/leverage values are read through ``api-demo``.
    The notional threshold is then tested on the actual order-create endpoint.
    A successful cancel request is not an accepted observation. The exact
    orderId/orderLinkId must subsequently be observed terminal ``Cancelled``
    with zero cumulative quantity/value and empty trade history on at least two
    bounded polls. Only documented 110094 lower-notional rejects are treated as
    search observations; every other failure aborts the probe.
    """

    if (
        not math.isfinite(terminal_history_timeout_seconds)
        or terminal_history_timeout_seconds <= 0.0
        or not math.isfinite(terminal_history_poll_seconds)
        or terminal_history_poll_seconds < 0.0
        or terminal_history_max_polls <= 0
        or terminal_confirmation_polls < 2
        or terminal_confirmation_polls > terminal_history_max_polls
    ):
        raise ValueError("terminal history bounds/confirmations are invalid")

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
    distance = Decimal(str(probe_distance_bps))
    if not distance.is_finite() or distance <= 0 or distance >= Decimal("10000"):
        raise ValueError("probe_distance_bps must be finite and in (0, 10000)")
    # One tick off the bid is unnecessarily risky when a full candidate
    # universe is probed serially. Move the buy away from the touch by a
    # recorded fixed percentage, rounded down to a tick. This is conservative
    # for quantity/notional discovery: the same quantity has no less notional
    # at the current touch. Any fill still fails the final flatness gate.
    raw_probe_price = bid * (Decimal("1") - distance / Decimal("10000"))
    probe_price = (raw_probe_price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
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

    def record_attempt(
        *,
        step_count: int,
        qty: Decimal,
        notional: Decimal,
        accepted_value: bool,
        outcome: str,
        rejection: str = "",
        order_link_id: str,
        **identity: Any,
    ) -> DemoRuleProbeAttempt:
        values: dict[str, Any] = {
            "step_count": step_count,
            "qty": float(qty),
            "notional_usdt": float(notional),
            "accepted": accepted_value,
            "outcome": outcome,
            "rejection": rejection[:1000],
            "order_link_id": order_link_id,
        }
        values.update(identity)
        attempt = DemoRuleProbeAttempt(**values)
        attempts.append(attempt)
        if attempt_sink is not None:
            attempt_sink.append(attempt)
        return attempt

    def accepted(step_count: int) -> bool:
        nonlocal attempt_number
        attempt_number += 1
        qty = step * step_count
        notional = qty * probe_price
        link = (
            f"lm-{link_namespace[:10]}-{symbol[:8]}-"
            f"{observed_ts_ns % 1_000_000_000:x}-{attempt_number}"
        )[:36]
        try:
            create_ack = client.place_order(
                symbol=symbol,
                side="Buy",
                orderType="Limit",
                qty=_text(qty),
                price=_text(probe_price),
                timeInForce="PostOnly",
                reduceOnly=False,
                orderLinkId=link,
            )
        except Exception as exc:  # noqa: BLE001 - classify the venue receipt below
            message = str(exc)
            if "110094" not in message and "notional value below" not in message.lower():
                record_attempt(
                    step_count=step_count,
                    qty=qty,
                    notional=notional,
                    accepted_value=False,
                    outcome="create_failed",
                    rejection=message,
                    order_link_id=link,
                )
                raise RuntimeError(f"{symbol}: non-threshold probe failure: {message}") from exc
            record_attempt(
                step_count=step_count,
                qty=qty,
                notional=notional,
                accepted_value=False,
                outcome="threshold_rejected",
                rejection=message[:500],
                order_link_id=link,
            )
            return False

        try:
            order_id = _ack_identity(
                create_ack,
                symbol=symbol,
                order_link_id=link,
                label=f"{symbol} create acknowledgement",
            )
        except _ProbeIdentityError as exc:
            record_attempt(
                step_count=step_count,
                qty=qty,
                notional=notional,
                accepted_value=False,
                outcome="verification_failed",
                rejection=str(exc),
                order_link_id=link,
            )
            raise RuntimeError(f"{symbol}: {exc}") from exc

        identity = {
            "order_id": order_id,
            "create_ack_source": ORDER_CREATE_SOURCE,
            "create_ack_order_id": order_id,
            "create_ack_order_link_id": link,
        }

        def fail_verification(message: str, **observed: Any) -> None:
            details = {**identity, **observed}
            record_attempt(
                step_count=step_count,
                qty=qty,
                notional=notional,
                accepted_value=False,
                outcome="verification_failed",
                rejection=message,
                order_link_id=link,
                **details,
            )
            raise RuntimeError(f"{symbol}: {message}")

        last_cancel_error: Exception | None = None
        for _ in range(10):
            try:
                cancel_ack = client.cancel_order(symbol=symbol, order_link_id=link)
                cancel_order_id = _ack_identity(
                    cancel_ack,
                    symbol=symbol,
                    order_link_id=link,
                    label=f"{symbol} cancel acknowledgement",
                )
                if cancel_order_id != order_id:
                    raise _ProbeIdentityError(
                        f"{symbol} cancel acknowledgement identity mismatch: "
                        f"expected {order_id!r}, observed {cancel_order_id!r}"
                    )
                break
            except _ProbeIdentityError as exc:
                fail_verification(str(exc))
            except Exception as exc:  # noqa: BLE001 - async ack/cancel race
                last_cancel_error = exc
                time.sleep(0.05)
        else:
            fail_verification(
                f"accepted probe order could not obtain a source-bound cancel acknowledgement: "
                f"{last_cancel_error}"
            )

        verified_identity = {
            **identity,
            "cancel_ack_source": ORDER_CANCEL_SOURCE,
            "cancel_ack_order_id": order_id,
            "cancel_ack_order_link_id": link,
            "order_history_source": ORDER_HISTORY_SOURCE,
            "order_history_query_symbol": symbol,
            "order_history_query_order_id": order_id,
            "order_history_query_order_link_id": link,
            "trade_history_source": TRADE_HISTORY_SOURCE,
            "trade_history_query_symbol": symbol,
            "trade_history_query_order_id": order_id,
            "trade_history_query_order_link_id": link,
        }
        deadline = time.monotonic() + terminal_history_timeout_seconds
        confirmations = 0
        last_status = ""
        last_cum_qty = ""
        last_cum_value = ""
        last_error = "terminal order history was not observable"
        last_trade_count = -1
        last_poll_count = 0
        for poll_number in range(1, terminal_history_max_polls + 1):
            if time.monotonic() > deadline:
                break
            last_poll_count = poll_number
            try:
                order_rows = client.get_order_history(
                    symbol=symbol,
                    order_id=order_id,
                    order_link_id=link,
                    limit=50,
                    max_pages=2,
                )
                if not isinstance(order_rows, list):
                    raise RuntimeError("order-history response must be a list")
                trade_rows = client.get_trade_history(
                    symbol=symbol,
                    order_id=order_id,
                    order_link_id=link,
                    limit=50,
                    max_pages=2,
                )
                if not isinstance(trade_rows, list):
                    raise RuntimeError("trade-history response must be a list")
            except Exception as exc:  # noqa: BLE001 - bounded retry, never a pass
                last_error = f"terminal verification read failed: {exc}"
                confirmations = 0
            else:
                last_trade_count = len(trade_rows)
                try:
                    for row in trade_rows:
                        _required_identity(
                            row,
                            symbol=symbol,
                            order_id=order_id,
                            order_link_id=link,
                            label=f"{symbol} trade-history row",
                        )
                except _ProbeIdentityError as exc:
                    fail_verification(
                        str(exc),
                        **verified_identity,
                        terminal_poll_count=poll_number,
                        terminal_confirmation_polls=confirmations,
                        trade_history_row_count=last_trade_count,
                    )
                if trade_rows:
                    execution_ids = sorted(
                        str(row.get("execId") or row.get("exec_id") or "")
                        for row in trade_rows
                        if isinstance(row, Mapping)
                    )
                    fail_verification(
                        "execution history proves the probe order traded "
                        f"(rows={len(trade_rows)}, execIds={execution_ids[:10]!r})",
                        **verified_identity,
                        terminal_poll_count=poll_number,
                        terminal_confirmation_polls=confirmations,
                        trade_history_row_count=last_trade_count,
                    )
                if len(order_rows) > 1:
                    fail_verification(
                        f"order-history query returned {len(order_rows)} rows for one exact identity",
                        **verified_identity,
                        terminal_poll_count=poll_number,
                        terminal_confirmation_polls=confirmations,
                        trade_history_row_count=0,
                    )
                if not order_rows:
                    last_error = "terminal order history was not observable"
                    confirmations = 0
                else:
                    row = order_rows[0]
                    try:
                        _required_identity(
                            row,
                            symbol=symbol,
                            order_id=order_id,
                            order_link_id=link,
                            label=f"{symbol} order-history row",
                        )
                    except _ProbeIdentityError as exc:
                        fail_verification(
                            str(exc),
                            **verified_identity,
                            terminal_poll_count=poll_number,
                            terminal_confirmation_polls=confirmations,
                            trade_history_row_count=0,
                        )
                    assert isinstance(row, Mapping)
                    last_status = str(row.get("orderStatus") or row.get("order_status") or "")
                    try:
                        last_cum_qty = _require_zero_decimal(
                            row.get("cumExecQty"),
                            label=f"{symbol} terminal cumExecQty",
                        )
                        last_cum_value = _require_zero_decimal(
                            row.get("cumExecValue"),
                            label=f"{symbol} terminal cumExecValue",
                        )
                    except RuntimeError as exc:
                        fail_verification(
                            str(exc),
                            **verified_identity,
                            terminal_order_id=order_id,
                            terminal_order_link_id=link,
                            terminal_status=last_status,
                            terminal_cum_exec_qty=str(row.get("cumExecQty") or ""),
                            terminal_cum_exec_value=str(row.get("cumExecValue") or ""),
                            terminal_observed_ts_ns=time.time_ns(),
                            terminal_poll_count=poll_number,
                            terminal_confirmation_polls=confirmations,
                            trade_history_row_count=0,
                        )
                    normalized_status = last_status.lower()
                    if last_status == "Cancelled":
                        confirmations += 1
                        if confirmations >= terminal_confirmation_polls:
                            record_attempt(
                                step_count=step_count,
                                qty=qty,
                                notional=notional,
                                accepted_value=True,
                                outcome="verified_cancelled_no_fill",
                                order_link_id=link,
                                **verified_identity,
                                terminal_order_id=order_id,
                                terminal_order_link_id=link,
                                terminal_status="Cancelled",
                                terminal_cum_exec_qty=last_cum_qty,
                                terminal_cum_exec_value=last_cum_value,
                                terminal_observed_ts_ns=time.time_ns(),
                                terminal_poll_count=poll_number,
                                terminal_confirmation_polls=confirmations,
                                trade_history_row_count=0,
                            )
                            return True
                    elif normalized_status in _PENDING_ORDER_STATUSES:
                        last_error = f"order remained non-terminal with status {last_status!r}"
                        confirmations = 0
                    else:
                        fail_verification(
                            f"order reached non-acceptable status {last_status!r}",
                            **verified_identity,
                            terminal_order_id=order_id,
                            terminal_order_link_id=link,
                            terminal_status=last_status,
                            terminal_cum_exec_qty=last_cum_qty,
                            terminal_cum_exec_value=last_cum_value,
                            terminal_observed_ts_ns=time.time_ns(),
                            terminal_poll_count=poll_number,
                            terminal_confirmation_polls=confirmations,
                            trade_history_row_count=0,
                        )
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                break
            if terminal_history_poll_seconds:
                time.sleep(min(terminal_history_poll_seconds, remaining))
        fail_verification(
            f"terminal Cancelled/no-fill verification timed out after "
            f"{last_poll_count}/{terminal_history_max_polls} bounded polls: {last_error}",
            **verified_identity,
            terminal_order_id=order_id if last_status else "",
            terminal_order_link_id=link if last_status else "",
            terminal_status=last_status,
            terminal_cum_exec_qty=last_cum_qty,
            terminal_cum_exec_value=last_cum_value,
            terminal_observed_ts_ns=time.time_ns(),
            terminal_poll_count=last_poll_count,
            terminal_confirmation_polls=confirmations,
            trade_history_row_count=last_trade_count,
        )
        raise AssertionError("unreachable")

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
        schema_version=DEMO_RULE_PROBE_EVIDENCE_SCHEMA_VERSION,
        kind=DEMO_RULE_PROBE_EVIDENCE_KIND,
        environment="demo",
        observed_ts_ns=observed_ts_ns,
        symbol=symbol,
        probe_price=float(probe_price),
        probe_distance_bps=float(distance),
        lowest_accepted_qty=float(accepted_qty),
        lowest_accepted_notional_usdt=float(accepted_notional),
        highest_rejected_qty=float(rejected_qty),
        highest_rejected_notional_usdt=float(rejected_notional),
        tested_leverage=tested_leverage,
        terminal_history_timeout_seconds=terminal_history_timeout_seconds,
        terminal_history_poll_seconds=terminal_history_poll_seconds,
        terminal_history_max_polls=terminal_history_max_polls,
        required_terminal_confirmation_polls=terminal_confirmation_polls,
        attempts=tuple(attempts),
    )
    if not math.isfinite(rule.min_notional) or rule.min_notional <= 0:
        raise RuntimeError(f"{symbol}: invalid observed minimum notional")
    validate_demo_rule_probe_evidence(
        evidence.to_dict(),
        expected_symbol=symbol,
        expected_observed_ts_ns=observed_ts_ns,
        expected_rules=rule,
        max_probe_notional_usdt=float(max_notional),
    )
    return rule, evidence
