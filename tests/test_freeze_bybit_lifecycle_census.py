from __future__ import annotations

import json

import pytest

from scripts.freeze_bybit_lifecycle_census import (
    _index_proxy,
    _search_result,
    _stable_search_hit,
)


def test_search_result_requires_complete_registered_page() -> None:
    raw = json.dumps(
        {
            "ret_code": 0,
            "ret_msg": "OK",
            "result": {
                "query": "AUSDT",
                "page": 0,
                "hitsPerPage": 50,
                "nbHits": 1,
                "hits": [{"objectID": "article.one", "url": "/article/one/"}],
            },
        }
    ).encode()

    total, hits = _search_result(raw, symbol="AUSDT", page=0)

    assert total == 1
    assert hits[0]["objectID"] == "article.one"

    incomplete = json.loads(raw)
    incomplete["result"]["nbHits"] = 2
    with pytest.raises(ValueError, match="page coverage changed"):
        _search_result(
            json.dumps(incomplete).encode(),
            symbol="AUSDT",
            page=0,
        )


def test_stable_search_hit_excludes_only_query_highlighting() -> None:
    base = {
        "objectID": "article.one",
        "title": "One article",
        "url": "/article/one/",
    }
    first = {**base, "_highlightResult": {"query": "AUSDT"}}
    second = {**base, "_highlightResult": {"query": "BUSDT"}}

    assert _stable_search_hit(first) == _stable_search_hit(second) == base
    assert _stable_search_hit({**second, "title": "Changed"}) != base


def test_index_proxy_requires_all_thirty_half_open_minutes() -> None:
    effective_ts_ms = 30 * 60_000
    rows = [
        [str(timestamp), "0", "0", "0", str(ordinal + 1)]
        for ordinal, timestamp in enumerate(range(0, effective_ts_ms, 60_000))
    ]
    raw = json.dumps(
        {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"symbol": "AUSDT", "list": list(reversed(rows))},
        }
    ).encode()

    proxy, decimal_proxy, canonical = _index_proxy(
        "AUSDT",
        effective_ts_ms,
        raw,
    )

    assert proxy == pytest.approx(15.5)
    assert decimal_proxy == "15.5"
    assert [row["ts_ms"] for row in canonical] == list(range(0, effective_ts_ms, 60_000))

    missing = json.loads(raw)
    missing["result"]["list"] = missing["result"]["list"][1:]
    with pytest.raises(ValueError, match="minute coverage changed"):
        _index_proxy("AUSDT", effective_ts_ms, json.dumps(missing).encode())
