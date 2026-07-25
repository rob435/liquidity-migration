from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from liquidity_migration.account_execution_config import load_demo_rules
from liquidity_migration.demo_rule_probe import (
    DEMO_RULE_PROBE_FAILURE_KIND,
    ORDER_HISTORY_SOURCE,
    ORDER_REALTIME_SOURCE,
    DemoRuleProbeAttempt,
    probe_demo_instrument_rule,
)
from liquidity_migration.deterministic_serialization import canonical_json


REPO_ROOT = Path(__file__).resolve().parents[1]


def _probe_script_module() -> Any:
    path = REPO_ROOT / "scripts" / "probe_bybit_demo_rules.py"
    spec = importlib.util.spec_from_file_location("probe_bybit_demo_rules_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _instrument() -> dict[str, Any]:
    return {
        "symbol": "BUSDT",
        "lotSizeFilter": {
            "qtyStep": "0.1",
            "minOrderQty": "0.1",
            "minNotionalValue": "1",
            "maxMktOrderQty": "100000",
        },
        "priceFilter": {"tickSize": "0.1"},
        "leverageFilter": {"maxLeverage": "25"},
    }


def test_probe_receipt_writer_preserves_existing_evidence(tmp_path: Path) -> None:
    module = _probe_script_module()
    output = tmp_path / "demo-rules.json"
    output.write_bytes(b"preserved prior evidence\n")
    output.chmod(0o600)
    payload = {"artifact_sha256": ""}
    payload["artifact_sha256"] = module._self_hash(payload)

    with pytest.raises(FileExistsError):
        module._write_private_receipt(output, payload)

    assert output.read_bytes() == b"preserved prior evidence\n"


def test_candidate_symbol_source_uses_the_exact_descriptor_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _probe_script_module()
    source = tmp_path / "candidate.json"
    source.write_text(
        json.dumps({"kind": module.CANDIDATE_UNIVERSE_KIND}) + "\n",
        encoding="utf-8",
    )
    source.chmod(0o600)
    source_bytes = source.read_bytes()

    def load_candidate(_path: Path, *, snapshot: Any) -> SimpleNamespace:
        assert snapshot.data == source_bytes
        return SimpleNamespace(
            symbols=("AAAUSDT",),
            path=source.absolute(),
            file_sha256=hashlib.sha256(source_bytes).hexdigest(),
            artifact_sha256="a" * 64,
        )

    monkeypatch.setattr(module, "load_candidate_universe", load_candidate)

    symbols, binding = module._symbols_from_file(source)

    assert symbols == ["AAAUSDT"]
    assert binding["sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert binding["artifact_self_hash_verified"] is True


class _ProbeClient:
    def __init__(
        self,
        *,
        threshold: float = 5.0,
        unknown_failure: bool = False,
        history_statuses: tuple[str, ...] = ("Cancelled",),
        trade_on_poll: int = 0,
        wrong_create_identity: bool = False,
        wrong_cancel_identity: bool = False,
        wrong_history_identity: bool = False,
        wrong_realtime_identity: bool = False,
        missing_history_identity: bool = False,
        missing_history_polls: int = 0,
        partial_fill: bool = False,
        realtime_statuses: tuple[str, ...] = (),
        realtime_partial_fill: bool = False,
    ) -> None:
        self.threshold = threshold
        self.unknown_failure = unknown_failure
        self.history_statuses = history_statuses
        self.trade_on_poll = trade_on_poll
        self.wrong_create_identity = wrong_create_identity
        self.wrong_cancel_identity = wrong_cancel_identity
        self.wrong_history_identity = wrong_history_identity
        self.wrong_realtime_identity = wrong_realtime_identity
        self.missing_history_identity = missing_history_identity
        self.missing_history_polls = missing_history_polls
        self.partial_fill = partial_fill
        self.realtime_statuses = realtime_statuses
        self.realtime_partial_fill = realtime_partial_fill
        self.accepted: list[str] = []
        self.cancelled: list[str] = []
        self.leverage: list[tuple[str, float, float]] = []
        self.order_ids: dict[str, str] = {}
        self.history_polls: dict[str, int] = {}

    def set_leverage(self, *, symbol: str, buy_leverage: float, sell_leverage: float) -> None:
        self.leverage.append((symbol, buy_leverage, sell_leverage))

    def place_order(self, **params: Any) -> dict[str, str]:
        if self.unknown_failure:
            raise RuntimeError("ErrCode: 10006 rate limit")
        if float(params["qty"]) * float(params["price"]) + 1e-12 < self.threshold:
            raise RuntimeError("Order notional value below the lower limit (ErrCode: 110094)")
        link = str(params["orderLinkId"])
        self.accepted.append(link)
        order_id = f"order-{len(self.accepted)}"
        self.order_ids[link] = order_id
        return {
            "symbol": "BUSDT",
            "orderId": order_id,
            "orderLinkId": "wrong" if self.wrong_create_identity else link,
        }

    def cancel_order(self, *, symbol: str, order_link_id: str) -> dict[str, str]:
        assert symbol == "BUSDT"
        self.cancelled.append(order_link_id)
        return {
            "symbol": symbol,
            "orderId": "wrong" if self.wrong_cancel_identity else self.order_ids[order_link_id],
            "orderLinkId": order_link_id,
        }

    def get_order_history(self, **params: Any) -> list[dict[str, str]]:
        order_id = str(params["order_id"])
        link = str(params["order_link_id"])
        assert params["symbol"] == "BUSDT"
        assert self.order_ids[link] == order_id
        poll = self.history_polls.get(order_id, 0) + 1
        self.history_polls[order_id] = poll
        if self.missing_history_identity or poll <= self.missing_history_polls:
            return []
        status = self.history_statuses[min(poll - 1, len(self.history_statuses) - 1)]
        return [{
            "symbol": "BUSDT",
            "orderId": "wrong" if self.wrong_history_identity else order_id,
            "orderLinkId": link,
            "orderStatus": status,
            "cumExecQty": "0.1" if self.partial_fill else "0",
            "cumExecValue": "0.99" if self.partial_fill else "0",
        }]

    def get_open_orders(self, **params: Any) -> list[dict[str, str]]:
        order_id = str(params["order_id"])
        link = str(params["order_link_id"])
        assert params["symbol"] == "BUSDT"
        assert params["settle_coin"] is None
        assert params["open_only"] == 1
        assert self.order_ids[link] == order_id
        if not self.realtime_statuses:
            return []
        poll = max(1, self.history_polls.get(order_id, 0))
        status = self.realtime_statuses[min(poll - 1, len(self.realtime_statuses) - 1)]
        return [{
            "symbol": "BUSDT",
            "orderId": "wrong" if self.wrong_realtime_identity else order_id,
            "orderLinkId": link,
            "orderStatus": status,
            "cumExecQty": "0.1" if self.realtime_partial_fill else "0",
            "cumExecValue": "0.99" if self.realtime_partial_fill else "0",
        }]

    def get_trade_history(self, **params: Any) -> list[dict[str, str]]:
        order_id = str(params["order_id"])
        link = str(params["order_link_id"])
        poll = self.history_polls.get(order_id, 0)
        if self.trade_on_poll and poll >= self.trade_on_poll:
            return [{
                "symbol": "BUSDT",
                "orderId": order_id,
                "orderLinkId": link,
                "execId": "exec-1",
            }]
        return []


def _probe(client: _ProbeClient, **kwargs: Any) -> Any:
    options: dict[str, Any] = {
        "terminal_history_timeout_seconds": 1.0,
        "terminal_history_poll_seconds": 0.0,
        "terminal_history_max_polls": 4,
    }
    options.update(kwargs)
    return probe_demo_instrument_rule(
        client,
        instrument_row=_instrument(),
        ticker_row={"symbol": "BUSDT", "bid1Price": "10.1"},
        observed_ts_ns=123456789,
        max_probe_notional_usdt=20.0,
        leverage=10.0,
        **options,
    )


def test_probe_finds_smallest_accepted_demo_qty_step() -> None:
    client = _ProbeClient(threshold=5.0)

    rule, evidence = _probe(client)

    assert rule.environment == "demo"
    assert rule.source == "bybit_demo_post_only_acceptance_probe"
    assert rule.qty_step == 0.1
    assert rule.min_qty == 0.1
    assert rule.min_notional == pytest.approx(5.94)
    assert evidence.probe_price == 9.9
    assert evidence.probe_distance_bps == 100.0
    assert evidence.lowest_accepted_qty == pytest.approx(0.6)
    assert evidence.highest_rejected_qty == pytest.approx(0.5)
    assert client.cancelled == client.accepted
    assert client.leverage == [("BUSDT", 10.0, 10.0)]
    accepted = [attempt for attempt in evidence.attempts if attempt.accepted]
    assert accepted
    assert all(attempt.outcome == "verified_cancelled_no_fill" for attempt in accepted)
    assert all(attempt.terminal_status == "Cancelled" for attempt in accepted)
    assert all(attempt.trade_history_row_count == 0 for attempt in accepted)


def test_prior_adjacent_bracket_revalidates_after_structural_hint_mismatch() -> None:
    rule, evidence = _probe(
        _ProbeClient(threshold=5.0),
        prior_bracket_qty=(0.5, 0.6),
    )

    assert rule.min_notional == pytest.approx(5.94)
    assert [attempt.step_count for attempt in evidence.attempts] == [1, 2, 5, 6]
    assert [attempt.accepted for attempt in evidence.attempts] == [False, False, False, True]


def test_current_structural_notional_hint_resolves_in_two_fresh_attempts() -> None:
    rule, evidence = _probe(_ProbeClient(threshold=1.0))

    assert rule.min_notional == pytest.approx(1.98)
    assert [attempt.step_count for attempt in evidence.attempts] == [1, 2]
    assert [attempt.accepted for attempt in evidence.attempts] == [False, True]


def test_prior_notional_bracket_is_rescaled_before_fresh_boundary_search() -> None:
    rule, evidence = _probe(
        _ProbeClient(threshold=5.0),
        # These quantities bracketed the same threshold at a lower historical
        # probe price. Reusing them directly would test two accepted orders and
        # fall back to a complete search at the current 9.9 probe price.
        prior_bracket_qty=(0.6, 0.7),
        prior_bracket_notional_usdt=(4.8, 5.6),
    )

    assert rule.min_notional == pytest.approx(5.94)
    assert [attempt.step_count for attempt in evidence.attempts] == [1, 2, 4, 6, 5]
    assert [attempt.accepted for attempt in evidence.attempts] == [
        False,
        False,
        False,
        True,
        False,
    ]


@pytest.mark.parametrize(
    ("threshold", "expected_qty", "expected_rejected_qty"),
    [
        (4.0, 0.5, 0.4),
        (6.5, 0.7, 0.6),
    ],
)
def test_changed_prior_boundary_falls_back_to_complete_search(
    threshold: float,
    expected_qty: float,
    expected_rejected_qty: float,
) -> None:
    rule, evidence = _probe(
        _ProbeClient(threshold=threshold),
        prior_bracket_qty=(0.5, 0.6),
    )

    assert evidence.lowest_accepted_qty == pytest.approx(expected_qty)
    assert evidence.highest_rejected_qty == pytest.approx(expected_rejected_qty)
    assert rule.min_notional == pytest.approx(expected_qty * 9.9)
    assert len(evidence.attempts) > 2


def test_prior_receipt_loader_treats_old_evidence_only_as_search_hints(
    tmp_path: Path,
) -> None:
    module = _probe_script_module()
    rule, evidence = _probe(_ProbeClient(threshold=5.0))
    payload = {
        "schema_version": module.DEMO_RULES_SCHEMA_VERSION,
        "kind": module.DEMO_RULES_KIND,
        "status": "passed",
        "environment": "demo",
        "verified_ts_ns": 123456789,
        "max_probe_notional_usdt": 20.0,
        "probe_distance_bps": 100.0,
        "max_private_requests_per_second": 5,
        "symbol_source": {"kind": "test"},
        "rules": {"BUSDT": asdict(rule)},
        "evidence": {"BUSDT": evidence.to_dict()},
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    prior = tmp_path / "prior-rules.json"
    prior.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    prior.chmod(0o600)

    brackets, identity = module._prior_probe_brackets(
        prior,
        expected_symbols=["BUSDT"],
    )

    assert brackets["BUSDT"] == pytest.approx((0.5, 0.6, 4.95, 5.94))
    assert identity["requested_symbol_count"] == 1
    assert identity["prior_symbol_count"] == 1
    assert identity["overlap_symbol_count"] == 1
    assert identity["missing_requested_symbols"] == []
    assert identity["retired_prior_symbols"] == []
    assert identity["role"] == "search_hints_only_revalidated_by_fresh_orders"


def test_prior_receipt_loader_uses_only_population_overlap_as_hints(
    tmp_path: Path,
) -> None:
    module = _probe_script_module()
    rule, evidence = _probe(_ProbeClient(threshold=5.0))
    payload = {
        "schema_version": module.DEMO_RULES_SCHEMA_VERSION,
        "kind": module.DEMO_RULES_KIND,
        "status": "passed",
        "environment": "demo",
        "verified_ts_ns": 123456789,
        "max_probe_notional_usdt": 20.0,
        "probe_distance_bps": 100.0,
        "max_private_requests_per_second": 5,
        "symbol_source": {"kind": "test"},
        "rules": {"BUSDT": asdict(rule)},
        "evidence": {"BUSDT": evidence.to_dict()},
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    prior = tmp_path / "prior-rules.json"
    prior.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    prior.chmod(0o600)

    brackets, identity = module._prior_probe_brackets(
        prior,
        expected_symbols=["BUSDT", "NEWUSDT"],
    )

    assert set(brackets) == {"BUSDT"}
    assert identity["overlap_symbol_count"] == 1
    assert identity["missing_requested_symbols"] == ["NEWUSDT"]
    assert identity["retired_prior_symbols"] == []


def test_probe_does_not_misclassify_transport_or_rate_failure_as_minimum() -> None:
    with pytest.raises(RuntimeError, match="non-threshold probe failure"):
        _probe(_ProbeClient(unknown_failure=True))


def test_probe_fails_when_explicit_cap_cannot_reach_demo_minimum() -> None:
    with pytest.raises(RuntimeError, match="no accepted order"):
        _probe(_ProbeClient(threshold=50.0))


def test_cancel_ack_still_new_times_out_without_false_acceptance() -> None:
    attempts: list[Any] = []
    with pytest.raises(RuntimeError, match="timed out"):
        _probe(
            _ProbeClient(threshold=1.0, history_statuses=("New",)),
            terminal_history_max_polls=2,
            attempt_sink=attempts,
        )
    assert attempts[-1].outcome == "verification_failed"
    assert attempts[-1].accepted is False
    assert attempts[-1].terminal_status == "New"


@pytest.mark.parametrize(
    ("client", "message"),
    [
        (_ProbeClient(threshold=1.0, partial_fill=True), "proves a probe fill"),
        (_ProbeClient(threshold=1.0, trade_on_poll=2), "execution history proves"),
    ],
)
def test_partial_or_late_fill_fails_probe(client: _ProbeClient, message: str) -> None:
    with pytest.raises(RuntimeError, match=message):
        _probe(client)


@pytest.mark.parametrize(
    "client",
    [
        _ProbeClient(threshold=1.0, wrong_create_identity=True),
        _ProbeClient(threshold=1.0, wrong_cancel_identity=True),
        _ProbeClient(threshold=1.0, wrong_history_identity=True),
        _ProbeClient(
            threshold=1.0,
            realtime_statuses=("Cancelled",),
            wrong_realtime_identity=True,
        ),
    ],
)
def test_wrong_order_or_link_identity_fails_probe(client: _ProbeClient) -> None:
    with pytest.raises(RuntimeError, match="identity mismatch"):
        _probe(client)


def test_missing_terminal_identity_never_becomes_accepted() -> None:
    with pytest.raises(RuntimeError, match="timed out"):
        _probe(
            _ProbeClient(threshold=1.0, missing_history_identity=True),
            terminal_history_max_polls=2,
        )


def test_realtime_cancel_proves_terminal_state_while_order_history_is_delayed() -> None:
    _, evidence = _probe(
        _ProbeClient(
            threshold=1.0,
            missing_history_identity=True,
            realtime_statuses=("Cancelled",),
        )
    )

    accepted = [attempt for attempt in evidence.attempts if attempt.accepted]
    assert accepted[-1].terminal_order_source == ORDER_REALTIME_SOURCE
    assert accepted[-1].terminal_confirmation_sources == (
        ORDER_REALTIME_SOURCE,
        ORDER_REALTIME_SOURCE,
    )
    assert accepted[-1].terminal_poll_count == 2


def test_realtime_fill_contradiction_fails_even_when_history_is_clean() -> None:
    with pytest.raises(RuntimeError, match="proves a probe fill"):
        _probe(
            _ProbeClient(
                threshold=1.0,
                history_statuses=("Cancelled",),
                realtime_statuses=("Cancelled",),
                realtime_partial_fill=True,
            )
        )


def test_eventual_clean_cancel_requires_two_terminal_confirmations() -> None:
    rule, evidence = _probe(
        _ProbeClient(
            threshold=1.0,
            history_statuses=("New", "Cancelled", "Cancelled"),
        )
    )
    assert rule.min_notional == pytest.approx(1.98)
    accepted = [attempt for attempt in evidence.attempts if attempt.accepted]
    assert accepted[-1].terminal_poll_count == 3
    assert accepted[-1].terminal_confirmation_polls == 2
    assert accepted[-1].terminal_confirmation_sources == (
        ORDER_HISTORY_SOURCE,
        ORDER_HISTORY_SOURCE,
    )


def test_default_window_accepts_delayed_terminal_history_only_after_two_confirmations() -> None:
    client = _ProbeClient(threshold=1.0, missing_history_polls=11)

    _, evidence = probe_demo_instrument_rule(
        client,
        instrument_row=_instrument(),
        ticker_row={"symbol": "BUSDT", "bid1Price": "10.1"},
        observed_ts_ns=123456789,
        max_probe_notional_usdt=20.0,
        terminal_history_poll_seconds=0.0,
    )

    accepted = [attempt for attempt in evidence.attempts if attempt.accepted]
    assert accepted[-1].terminal_poll_count == 13
    assert accepted[-1].terminal_confirmation_polls == 2
    assert accepted[-1].terminal_status == "Cancelled"
    assert accepted[-1].terminal_cum_exec_qty == "0"
    assert accepted[-1].terminal_cum_exec_value == "0"
    assert accepted[-1].trade_history_row_count == 0
    assert evidence.terminal_history_timeout_seconds == 30.0
    assert evidence.terminal_history_max_polls == 100


@pytest.mark.parametrize("distance", [0.0, -1.0, 10_000.0, float("inf")])
def test_probe_distance_must_be_positive_and_below_full_price(distance: float) -> None:
    with pytest.raises(ValueError, match="probe_distance_bps"):
        probe_demo_instrument_rule(
            _ProbeClient(),
            instrument_row=_instrument(),
            ticker_row={"symbol": "BUSDT", "bid1Price": "10.1"},
            observed_ts_ns=123456789,
            max_probe_notional_usdt=20.0,
            probe_distance_bps=distance,
        )


def test_probe_cli_checks_explicit_conditional_order_view() -> None:
    text = (REPO_ROOT / "scripts" / "probe_bybit_demo_rules.py").read_text()

    assert 'client.get_open_orders(settle_coin="USDT")' in text
    assert 'order_filter="StopOrder"' in text
    assert "_open_orders_all_kinds(client)" in text
    assert "client.get_tickers(symbol=symbol)" in text
    assert "single all-symbol snapshot" in text
    assert "eta_seconds=" in text
    assert "DemoAccountIdentity.from_api_key_info" in text
    assert "DemoAccountMutationLease(identity)" in text
    assert "--owner-lock" not in text
    assert "--account-root" not in text
    assert '"artifact_sha256": ""' in text
    assert "os.fsync" in text


@pytest.mark.parametrize(
    "arguments",
    [
        ["--max-probe-notional-usdt", "201"],
        ["--probe-distance-bps", "50"],
        ["--max-private-requests-per-second", "11"],
        ["--leverage", "11"],
        ["--leverage", "nan"],
    ],
)
def test_probe_cli_rejects_parameters_outside_registered_contract_before_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    module = _probe_script_module()
    monkeypatch.setattr(
        module,
        "resolve_demo_credentials",
        lambda: pytest.fail("credentials must not be read for an invalid probe contract"),
    )
    output = tmp_path / "rules.json"

    with pytest.raises(SystemExit) as raised:
        module.main([
            "--symbols",
            "BUSDT",
            "--output",
            str(output),
            "--confirm-demo-probe",
            *arguments,
        ])

    assert raised.value.code == 2
    assert not output.exists()
    assert list(tmp_path.glob("rules.json.failed-*.json")) == []


def test_probe_symbol_file_binds_and_verifies_self_hashed_universe(tmp_path: Path) -> None:
    module = _probe_script_module()
    payload = {
        "schema_version": 1,
        "symbols": ["ethusdt", "BTCUSDT", "BTCUSDT"],
        "artifact_sha256": "",
    }
    payload["artifact_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    source = tmp_path / "candidate-universe.json"
    source.write_text(json.dumps(payload, sort_keys=True) + "\n")

    symbols, identity = module._symbols_from_file(source)

    assert symbols == ["BTCUSDT", "ETHUSDT"]
    assert identity["path"] == str(source.resolve())
    assert identity["artifact_self_hash_verified"] is True
    assert identity["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_probe_symbol_file_rejects_tampered_or_symlink_source(tmp_path: Path) -> None:
    module = _probe_script_module()
    source = tmp_path / "candidate-universe.json"
    source.write_text('{"symbols":["BTCUSDT"],"artifact_sha256":"' + "0" * 64 + '"}\n')
    with pytest.raises(ValueError, match="artifact_sha256"):
        module._symbols_from_file(source)

    plain = tmp_path / "symbols.txt"
    plain.write_text("BTCUSDT\n")
    alias = tmp_path / "symbols-link.txt"
    alias.symlink_to(plain)
    with pytest.raises(ValueError, match="symbolic link"):
        module._symbols_from_file(alias)


def test_probe_cleanup_recovers_but_reports_any_fill_position() -> None:
    module = _probe_script_module()

    class Client:
        position = {"symbol": "BUSDT", "side": "Buy", "size": "0.5"}

        def get_open_orders(self, **_params: Any) -> list[dict[str, Any]]:
            return []

        def get_positions(self, **_params: Any) -> list[dict[str, Any]]:
            return [] if self.position is None else [self.position]

        def place_order(self, **params: Any) -> dict[str, str]:
            assert params["reduceOnly"] is True
            assert params["side"] == "Sell"
            self.position = None
            return {"orderId": "recovery"}

    client = Client()
    observed = module._cleanup_probe_state(client)

    assert observed["status"] == "failed"
    assert observed["positions_observed"] == [
        {"symbol": "BUSDT", "side": "Buy", "size": "0.5"}
    ]
    assert observed["final_flatness"]["flat"] is True
    assert client.position is None


def test_probe_script_retains_private_self_hashed_failure_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _probe_script_module()

    class Client:
        def get_api_key_information(self) -> dict[str, Any]:
            return {"apiKey": "demo-key", "userID": "42"}

        def get_positions(self, **_params: Any) -> list[dict[str, Any]]:
            return []

        def get_open_orders(self, **_params: Any) -> list[dict[str, Any]]:
            return []

        def get_instruments_info(self) -> list[dict[str, Any]]:
            return [_instrument()]

        def get_tickers(self, **_params: Any) -> list[dict[str, Any]]:
            return [{"symbol": "BUSDT", "bid1Price": "10.1"}]

    class Identity:
        environment = "demo"
        user_id = "42"
        api_key_sha256 = "a" * 64

    class IdentityFactory:
        @staticmethod
        def from_api_key_info(**_params: Any) -> Identity:
            return Identity()

    class Lease:
        def __init__(self, _identity: Any) -> None:
            self.acquired = False

        def acquire(self) -> None:
            self.acquired = True

        def close(self) -> None:
            self.acquired = False

    def fail_probe(*_args: Any, **kwargs: Any) -> Any:
        kwargs["attempt_sink"].append(DemoRuleProbeAttempt(
            step_count=1,
            qty=0.1,
            notional_usdt=0.99,
            accepted=False,
            outcome="create_failed",
            rejection="synthetic failure",
            order_link_id="lm-demo-rule-BUSDT-test-1",
        ))
        raise RuntimeError("synthetic probe failure")

    client = Client()
    monkeypatch.setattr(module, "validate_demo_order_permission", lambda **_params: None)
    monkeypatch.setattr(module, "resolve_demo_credentials", lambda: ("demo-key", "secret"))
    monkeypatch.setattr(module, "api_key_allows_order_submit", lambda _row: (True, ""))
    monkeypatch.setattr(module, "BybitRestRateLimiter", lambda **_params: object())
    monkeypatch.setattr(module, "BybitPrivateClient", lambda **_params: client)
    monkeypatch.setattr(module, "DemoAccountIdentity", IdentityFactory)
    monkeypatch.setattr(module, "DemoAccountMutationLease", Lease)
    monkeypatch.setattr(module, "probe_demo_instrument_rule", fail_probe)

    output = tmp_path / "demo-rules.json"
    with pytest.raises(RuntimeError, match="synthetic probe failure"):
        module.main([
            "--symbols",
            "BUSDT",
            "--output",
            str(output),
            "--confirm-demo-probe",
        ])

    failures = list(tmp_path.glob("demo-rules.json.failed-*.json"))
    assert len(failures) == 1
    failure = failures[0]
    assert os.stat(failure).st_mode & 0o777 == 0o600
    payload = json.loads(failure.read_text(encoding="utf-8"))
    assert payload["kind"] == DEMO_RULE_PROBE_FAILURE_KIND
    assert payload["status"] == "failed"
    assert payload["cleanup"]["status"] == "passed"
    assert payload["final_flatness"]["flat"] is True
    assert payload["partial_attempts"]["BUSDT"][0]["accepted"] is False
    assert payload["artifact_sha256"] == hashlib.sha256(
        canonical_json({**payload, "artifact_sha256": ""})
    ).hexdigest()
    with pytest.raises(ValueError, match="passed source-bound"):
        load_demo_rules(failure)
