from __future__ import annotations

import importlib.util
import io
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "refresh_binance_metrics",
    Path(__file__).resolve().parents[2] / "scripts/data/refresh_binance_metrics.py",
)
assert SPEC is not None and SPEC.loader is not None
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def test_archive_url_encodes_unicode_symbol_without_changing_disk_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(metrics, "RAW", tmp_path)
    symbol = "哈基米USDT"
    payload = b"retained archive bytes"

    def read(url, *, timeout):
        assert url.isascii()
        assert url == (
            "https://data.binance.vision/data/futures/um/daily/metrics/"
            "%E5%93%88%E5%9F%BA%E7%B1%B3USDT/"
            "%E5%93%88%E5%9F%BA%E7%B1%B3USDT-metrics-2026-09-07.zip"
        )
        assert timeout == 60
        return io.BytesIO(payload)

    monkeypatch.setattr(metrics.urllib.request, "urlopen", read)
    assert metrics.fetch_one((symbol, "2026-09-07")) == "ok"
    assert (tmp_path / symbol / f"{symbol}-metrics-2026-09-07.zip").read_bytes() == payload
