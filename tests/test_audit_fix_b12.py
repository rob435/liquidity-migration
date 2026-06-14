"""Regression tests for audit bucket b12.

Covers: cli-config-2, cli-config-3, cli-config-4, cli-config-7, code-quality-9,
cost-funding-1 (config.py); ingestion-1, ingestion-5, ratelimit-rest-6
(binance.py); shadows-3, shadows-4 (continuous_addon_shadow.py).

Each test would FAIL on the original code and PASS on the fix.
"""

from __future__ import annotations

import argparse
import io
from dataclasses import replace
from pathlib import Path
from urllib.error import HTTPError

import pytest

from liquidity_migration import binance, continuous_addon_shadow as cas
from liquidity_migration.cli import (
    _KNOWN_BINANCE_PROXY_DATASETS,
    _KNOWN_BYBIT_DATASETS,
    _parse_symbols,
    _universe_config_from_args,
    _validate_datasets,
    build_parser,
)
from liquidity_migration.config import DEFAULT_EXCLUDED_SYMBOLS, CostConfig, UniverseConfig


# --------------------------------------------------------------------------- #
# cli-config-2 / cost-funding-1: CostConfig default must model 100% taker.
# --------------------------------------------------------------------------- #
def test_cost_config_default_is_full_taker_not_maker_blend() -> None:
    # The default must NOT be the 0.60 maker blend (9.6 bps) that under-costs by
    # ~36% and silently flatters returns. It must be the deployed 100%-taker cost.
    assert CostConfig().maker_fill_probability == pytest.approx(0.0)
    assert CostConfig().base_entry_exit_cost_bps == pytest.approx(15.0)
    # A maker blend must still be expressible when explicitly opted into.
    assert CostConfig(maker_fill_probability=0.60).base_entry_exit_cost_bps == pytest.approx(9.6)
    # The default must never be cheaper than the explicit full-taker cost.
    assert CostConfig().base_entry_exit_cost_bps == pytest.approx(
        replace(CostConfig(), maker_fill_probability=0.0).base_entry_exit_cost_bps
    )


# --------------------------------------------------------------------------- #
# cli-config-3: event-risk-cycle --loop must fail fast on order-safety misconfig.
# --------------------------------------------------------------------------- #
def test_event_risk_cycle_loop_validates_before_spinning(monkeypatch, tmp_path: Path) -> None:
    from liquidity_migration import cli

    # --submit-orders without --confirm-demo-orders is a fatal misconfig. In
    # --loop mode it previously got swallowed by the per-cycle broad except and
    # spun forever. It must now raise BEFORE the loop and never build a client or
    # run a cycle.
    built_client = {"count": 0}
    ran_cycle = {"count": 0}

    def fake_build_client(config, risk_config):  # pragma: no cover - must not run
        built_client["count"] += 1
        return object()

    def fake_run_cycle(*args, **kwargs):  # pragma: no cover - must not run
        ran_cycle["count"] += 1
        return {}

    monkeypatch.setattr(cli, "build_event_risk_private_client", fake_build_client)
    monkeypatch.setattr(cli, "run_event_risk_cycle", fake_run_cycle)

    argv = [
        "--data-root",
        str(tmp_path),
        "event-risk-cycle",
        "--loop",
        "--submit-orders",
        "--max-cycles",
        "0",
        "--interval-seconds",
        "0.0",
    ]
    with pytest.raises(RuntimeError, match="confirm-demo-orders"):
        cli.main(argv)
    # The fatal config error must surface before the loop arms anything.
    assert built_client["count"] == 0
    assert ran_cycle["count"] == 0


# --------------------------------------------------------------------------- #
# cli-config-4: unknown/typo'd --datasets must fail loud, not silently no-op.
# --------------------------------------------------------------------------- #
def test_validate_datasets_rejects_unknown_bybit_dataset() -> None:
    with pytest.raises(RuntimeError, match="funidng"):
        _validate_datasets({"klines_1h", "funidng"}, _KNOWN_BYBIT_DATASETS, venue="Bybit")
    # Every known token passes unchanged.
    known = {"instruments", "klines_1h", "archive_klines_1m"}
    assert _validate_datasets(known, _KNOWN_BYBIT_DATASETS, venue="Bybit") == known


def test_validate_datasets_accepts_binance_alias_and_canonical_names() -> None:
    # Aliases (map keys) and already-resolved binance_usdm_* names both pass.
    assert _validate_datasets({"funding"}, _KNOWN_BINANCE_PROXY_DATASETS, venue="Binance proxy") == {"funding"}
    assert _validate_datasets(
        {"binance_usdm_funding"}, _KNOWN_BINANCE_PROXY_DATASETS, venue="Binance proxy"
    ) == {"binance_usdm_funding"}
    with pytest.raises(RuntimeError, match="klines_1hr"):
        _validate_datasets({"klines_1hr"}, _KNOWN_BINANCE_PROXY_DATASETS, venue="Binance proxy")


def test_download_command_defaults_are_known_datasets() -> None:
    # The committed argparse defaults must not trip the new validation.
    bybit_default = {item.strip() for item in "instruments,klines_1h".split(",")}
    assert not (bybit_default - _KNOWN_BYBIT_DATASETS)
    proxy_default = {
        item.strip()
        for item in "klines_1h,funding,mark_price_1h,index_price_1h,premium_index_1h".split(",")
    }
    assert not (proxy_default - _KNOWN_BINANCE_PROXY_DATASETS)


# --------------------------------------------------------------------------- #
# cli-config-7: contradictory --include-excluded + --exclude-defaults must error.
# --------------------------------------------------------------------------- #
def _universe_args(**overrides) -> argparse.Namespace:
    base = dict(
        exclude_symbols=None,
        include_majors=False,
        exclude_majors=False,
        min_turnover_24h=None,
        min_age_days=None,
        max_age_days=None,
        rank_start=None,
        rank_end=None,
        max_symbols=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_universe_config_rejects_contradictory_include_and_exclude() -> None:
    base = UniverseConfig()
    with pytest.raises(RuntimeError, match="mutually exclusive"):
        _universe_config_from_args(base, _universe_args(include_majors=True, exclude_majors=True))


def test_universe_config_single_flag_still_resolves() -> None:
    base = UniverseConfig()
    inc = _universe_config_from_args(base, _universe_args(include_majors=True))
    assert inc.exclude_symbols == ()
    exc = _universe_config_from_args(base, _universe_args(exclude_majors=True))
    assert exc.exclude_symbols == DEFAULT_EXCLUDED_SYMBOLS


def test_discover_universe_parser_rejects_both_exclusion_flags_at_parse() -> None:
    # cli-config-7 (see test_audit_int_iK) made the four exclusion flags a parse-time
    # mutually-exclusive group, so passing both is now an argparse error (SystemExit)
    # BEFORE runtime. The runtime guard in _universe_config_from_args remains as
    # defense-in-depth for a programmatic caller and is pinned above via _universe_args.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["discover-universe", "--include-excluded", "--exclude-defaults"])


# --------------------------------------------------------------------------- #
# code-quality-9: single symbol-parsing helper used by every download branch.
# --------------------------------------------------------------------------- #
def test_parse_symbols_strips_and_uppercases() -> None:
    assert _parse_symbols(" btcusdt, ethusdt ,, solusdt ") == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    assert _parse_symbols("") == []
    assert _parse_symbols(None) == []


# --------------------------------------------------------------------------- #
# ingestion-1: a mid-range empty page after a full page must NOT silently
# truncate; it raises so the downloader does not mark an incomplete range done.
# --------------------------------------------------------------------------- #
class _FakeJsonResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._payload


def _kline_row(ts: int) -> list:
    # Binance kline row: open time first, rest are price/volume placeholders.
    return [ts, "1", "1", "1", "1", "1", ts + 1, "1", 0, "1", "1", "0"]


def test_paged_kline_raises_on_suspicious_mid_range_empty_page(monkeypatch) -> None:
    import json

    interval_ms = binance.BINANCE_INTERVAL_MS["1h"]
    start = 0
    end = 10 * interval_ms
    page_limit = 3

    # Page 1 returns a FULL page (3 rows), page 2 returns [] while the cursor is
    # still far short of end -> silent truncation signal -> must raise.
    pages = [
        [_kline_row(0), _kline_row(interval_ms), _kline_row(2 * interval_ms)],
        [],
    ]

    def fake_urlopen(request, timeout):
        batch = pages.pop(0) if pages else []
        return _FakeJsonResponse(json.dumps(batch).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda *_a, **_k: None)

    client = binance.BinanceUSDMData(retries=1, retry_sleep_seconds=0.0)
    with pytest.raises(binance.BinanceDataError, match="empty page"):
        client.get_klines("BTCUSDT", "1h", start, end, limit=page_limit)


def test_paged_kline_terminal_empty_page_after_partial_is_benign(monkeypatch) -> None:
    import json

    interval_ms = binance.BINANCE_INTERVAL_MS["1h"]
    start = 0
    end = 10 * interval_ms
    page_limit = 100  # rows < limit -> partial page -> not a truncation signal

    pages = [
        [_kline_row(0), _kline_row(interval_ms)],  # partial page (2 < 100)
        [],  # benign terminal empty page
    ]

    def fake_urlopen(request, timeout):
        batch = pages.pop(0) if pages else []
        return _FakeJsonResponse(json.dumps(batch).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda *_a, **_k: None)

    client = binance.BinanceUSDMData(retries=1, retry_sleep_seconds=0.0)
    rows = client.get_klines("BTCUSDT", "1h", start, end, limit=page_limit)
    assert [int(r[0]) for r in rows] == [0, interval_ms]


def test_raise_if_suspicious_empty_page_signal_logic() -> None:
    # Full prior page, cursor well short of end -> raise.
    with pytest.raises(binance.BinanceDataError):
        binance._raise_if_suspicious_empty_page(
            "/p", "BTCUSDT", cursor=1000, end=1_000_000, step_ms=3600, prev_page_full=True
        )
    # Prior page NOT full -> benign terminator, no raise.
    binance._raise_if_suspicious_empty_page(
        "/p", "BTCUSDT", cursor=1000, end=1_000_000, step_ms=3600, prev_page_full=False
    )
    # Full prior page but the next bar lands beyond end -> nothing truncated.
    binance._raise_if_suspicious_empty_page(
        "/p", "BTCUSDT", cursor=999_999, end=1_000_000, step_ms=3600, prev_page_full=True
    )
    # Irregular cadence (step_ms None, e.g. funding): full prior page still raises.
    with pytest.raises(binance.BinanceDataError):
        binance._raise_if_suspicious_empty_page(
            "/p", "BTCUSDT", cursor=1000, end=1_000_000, step_ms=None, prev_page_full=True
        )


# --------------------------------------------------------------------------- #
# ingestion-5: a permanent 4xx (non-429) error must fail fast, not burn retries.
# --------------------------------------------------------------------------- #
def _http_error(code: int, body: bytes = b"", headers=None) -> HTTPError:
    return HTTPError(
        url="http://x",
        code=code,
        msg="err",
        hdrs=headers,
        fp=io.BytesIO(body),
    )


def test_http_400_invalid_symbol_is_not_retried(monkeypatch) -> None:
    calls = {"urlopen": 0, "sleep": 0}

    def fake_urlopen(request, timeout):
        calls["urlopen"] += 1
        raise _http_error(400, b'{"code": -1121, "msg": "Invalid symbol."}')

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda *_a, **_k: calls.__setitem__("sleep", calls["sleep"] + 1))

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.0)
    with pytest.raises(binance.BinanceDataError, match="HTTP 400"):
        client._get("/fapi/v1/klines", {"symbol": "BAD"})

    # Exactly one call, no retries, no backoff sleeps.
    assert calls == {"urlopen": 1, "sleep": 0}
    assert client.calls == 1
    assert client.retry_events == 0


def test_http_500_is_still_retried(monkeypatch) -> None:
    calls = {"urlopen": 0}

    def fake_urlopen(request, timeout):
        calls["urlopen"] += 1
        raise _http_error(500, b"server error")

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda *_a, **_k: None)

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.0)
    with pytest.raises(binance.BinanceDataError, match="after retries"):
        client._get("/fapi/v1/klines", {"symbol": "X"})
    # 5xx is transient -> all retries consumed.
    assert calls["urlopen"] == 3
    assert client.retry_events == 2


# --------------------------------------------------------------------------- #
# ratelimit-rest-6: 429/418 must honor Retry-After (capped) before retrying.
# --------------------------------------------------------------------------- #
class _Headers:
    def __init__(self, mapping):
        self._m = mapping

    def get(self, key, default=None):
        return self._m.get(key, default)


def test_429_honors_retry_after_header(monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429, b"", headers=_Headers({"Retry-After": "7"}))
        import json

        return _FakeJsonResponse(json.dumps([]).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda s: sleeps.append(s))

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.5)
    client._get("/fapi/v1/klines", {"symbol": "X"})
    # First (and only) backoff must reflect the 7s Retry-After, not the 0.5s base.
    assert sleeps and sleeps[0] == pytest.approx(7.0)


def test_429_retry_after_is_capped(monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429, b"", headers=_Headers({"Retry-After": "99999"}))
        import json

        return _FakeJsonResponse(json.dumps([]).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda s: sleeps.append(s))

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.5, max_retry_after_seconds=120.0)
    client._get("/fapi/v1/klines", {"symbol": "X"})
    assert sleeps and sleeps[0] == pytest.approx(120.0)


def test_429_without_retry_after_uses_rate_limit_backoff(monkeypatch) -> None:
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fake_urlopen(request, timeout):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429, b"", headers=_Headers({}))
        import json

        return _FakeJsonResponse(json.dumps([]).encode("utf-8"))

    monkeypatch.setattr(binance, "urlopen", fake_urlopen)
    monkeypatch.setattr(binance.time, "sleep", lambda s: sleeps.append(s))

    client = binance.BinanceUSDMData(retries=3, retry_sleep_seconds=0.5, rate_limit_backoff_seconds=30.0)
    client._get("/fapi/v1/klines", {"symbol": "X"})
    # No Retry-After -> the dedicated rate-limit fallback (30s), not the 0.5s base.
    assert sleeps and sleeps[0] == pytest.approx(30.0)


# --------------------------------------------------------------------------- #
# shadows-3: same primary/addon source must fail loud (silent 2x double-count).
# --------------------------------------------------------------------------- #
def _write_addon_fixture(root: Path, trades: list[dict], orders: list[dict]) -> None:
    from liquidity_migration.storage import write_dataset
    import polars as pl

    write_dataset(pl.DataFrame(trades), root, "continuous_fade_paper_trades", partition_by=())
    write_dataset(pl.DataFrame(orders), root, "continuous_fade_paper_orders", partition_by=())
    write_dataset(
        pl.DataFrame([{"ts_ms": 1_000, "entry_candidates": 1, "entries": 1}]),
        root,
        "continuous_fade_paper_cycles",
        partition_by=(),
    )


def test_addon_audit_same_root_and_dataset_raises(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    trade = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": 1_000,
        "entry_ts_ms": 1_000 + 2 * 3_600_000,
        "trade_id": "t1",
        "strategy_id": "primary",
    }
    order = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": 1_000,
        "trade_id": "t1",
        "status": "filled",
        "order_link_id": "lm-en-ca-1",
        "strategy_id": "primary",
    }
    _write_addon_fixture(root, [trade], [order])

    config = cas.ContinuousAddonShadowAuditConfig(
        primary_data_root=str(root),
        addon_data_root=str(root),
        output_dir=str(tmp_path / "out"),
    )
    with pytest.raises(ValueError, match="same trades source"):
        cas.run_continuous_addon_shadow_audit(config)


def test_addon_audit_same_root_with_strategy_split_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    rows = [
        {
            "symbol": "BTCUSDT",
            "signal_ts_ms": 1_000,
            "entry_ts_ms": 1_000 + 2 * 3_600_000,
            "trade_id": "p1",
            "strategy_id": "primary_strat",
        },
        {
            "symbol": "ETHUSDT",
            "signal_ts_ms": 2_000,
            "entry_ts_ms": 2_000 + 2 * 3_600_000,
            "trade_id": "a1",
            "strategy_id": "addon_strat",
        },
    ]
    orders = [
        {
            "symbol": "BTCUSDT",
            "signal_ts_ms": 1_000,
            "trade_id": "p1",
            "status": "filled",
            "order_link_id": "lm-en-ca-p",
            "strategy_id": "primary_strat",
        },
        {
            "symbol": "ETHUSDT",
            "signal_ts_ms": 2_000,
            "trade_id": "a1",
            "status": "filled",
            "order_link_id": "lm-en-ca-a",
            "strategy_id": "addon_strat",
        },
    ]
    _write_addon_fixture(root, rows, orders)

    config = cas.ContinuousAddonShadowAuditConfig(
        primary_data_root=str(root),
        addon_data_root=str(root),
        expected_primary_strategy_id="primary_strat",
        expected_addon_strategy_id="addon_strat",
        output_dir=str(tmp_path / "out"),
    )
    # Must not raise, and the two sides must be disjoint (1 trade each), not 2/2.
    payload = cas.run_continuous_addon_shadow_audit(config)
    assert payload["summary"]["primary"]["trades"] == 1
    assert payload["summary"]["addon"]["trades"] == 1


# --------------------------------------------------------------------------- #
# shadows-4: a trade keyed by entry_ts_ms (signal_ts_ms dropped) must not flag
# its signal-keyed order as an unmatched entry-order attempt.
# --------------------------------------------------------------------------- #
def test_reconciliation_signal_ts_has_no_entry_ts_fallback() -> None:
    # Healthy rows carry signal_ts_ms -> identical to _signal_ts.
    healthy = {"signal_ts_ms": 5_000, "entry_ts_ms": 9_000}
    assert cas._reconciliation_signal_ts(healthy) == 5_000
    # A row that dropped the signal fields must NOT fall back to entry_ts_ms
    # (that mis-keys the reconciliation, the shadows-4 bug).
    degenerate = {"entry_ts_ms": 9_000}
    assert cas._reconciliation_signal_ts(degenerate) == 0
    assert cas._signal_ts(degenerate) == 9_000  # legacy fallback unchanged


def test_signal_less_trade_does_not_falsely_flag_its_order_unmatched() -> None:
    # Entry occurs 2 bars after the signal, so entry_ts_ms != signal_ts_ms.
    signal_ts = 1_000_000
    entry_ts = signal_ts + 2 * 3_600_000
    # Trade DROPPED signal_ts_ms (future schema / crash-recovery row) but keeps
    # entry_ts_ms and the authoritative trade_id.
    trade = {"symbol": "BTCUSDT", "entry_ts_ms": entry_ts, "trade_id": "tid-1"}
    # Its order carries signal_ts_ms and the same trade_id.
    order = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": signal_ts,
        "trade_id": "tid-1",
        "status": "filled",
        "order_link_id": "lm-en-ca-1",
    }

    summary = cas._entry_order_trade_reconciliation_summary(order_rows=[order], trade_rows=[trade])
    # trade_id reconciliation must match it -> NOT flagged unmatched.
    assert summary["entry_order_attempts_without_trade_key"] == 0
    assert summary["live_entry_order_attempts_without_trade_key"] == 0


def test_genuinely_unmatched_order_is_still_flagged() -> None:
    # An order with a valid signal key but no corresponding trade (no trade_id
    # match, no signal-key match) must still be counted unmatched.
    order = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": 1_000_000,
        "trade_id": "orphan",
        "status": "filled",
        "order_link_id": "lm-en-ca-9",
    }
    summary = cas._entry_order_trade_reconciliation_summary(order_rows=[order], trade_rows=[])
    assert summary["entry_order_attempts_without_trade_key"] == 1


def test_healthy_signal_keyed_trade_and_order_reconcile() -> None:
    # The live path: trade and order share signal_ts_ms -> reconcile on the key.
    signal_ts = 1_000_000
    trade = {"symbol": "BTCUSDT", "signal_ts_ms": signal_ts, "entry_ts_ms": signal_ts + 3_600_000}
    order = {
        "symbol": "BTCUSDT",
        "signal_ts_ms": signal_ts,
        "status": "filled",
        "order_link_id": "lm-en-ca-1",
    }
    summary = cas._entry_order_trade_reconciliation_summary(order_rows=[order], trade_rows=[trade])
    assert summary["entry_order_attempts_without_trade_key"] == 0
