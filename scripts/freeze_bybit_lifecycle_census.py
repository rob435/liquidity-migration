#!/usr/bin/env python3
"""Freeze official Bybit delisting events for the structural comparator."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import http.cookiejar
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import polars as pl

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from liquidity_migration._common import MS_PER_HOUR  # noqa: E402
from liquidity_migration.strategy_funnel import payload_sha256  # noqa: E402
from liquidity_migration.venue_lifecycle import (  # noqa: E402
    DELISTING_PROXY_EXACTNESS,
    DELISTING_PROXY_METHOD,
)

EPOCH_ROOT = REPO / "reports/prospective-runtime-parity-execution-epoch-2026-07-18"
DEFAULT_MANIFEST_ROOT = EPOCH_ROOT / "reconstructed/bybit-baseline/archive_trade_manifest"
DEFAULT_LINKMAP_ROOT = EPOCH_ROOT / "venue-lifecycle/bybit-census/source/linkmap"
DEFAULT_OUT = EPOCH_ROOT / "venue-lifecycle/bybit-census-search-v2"
REGISTERED_LINKMAP_PAGES = 12
REGISTERED_END_DATE = "2026-07-09"
REGISTERED_END_MS = 1_783_641_600_000
USER_AGENT = "Mozilla/5.0 (compatible; liquidity-migration-research/1.0)"
SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
SEARCH_ENDPOINT = "https://announcements.bybit.com/x-api/announcements/api/search/v1/index/announcement-posts_en"
SEARCH_BOOTSTRAP_URL = "https://announcements.bybit.com/en/article_filter/KLAYUSDT/"
SEARCH_BUILD_ID = "Qy-gplcCtv3r30Ts8tjHv"
SEARCH_BUILD_MANIFEST_URL = (
    f"https://announcements.bybit.com/static/announcements/_next/static/{SEARCH_BUILD_ID}/_buildManifest.js"
)
SEARCH_ARTICLE_FILTER_CHUNK_URL = (
    "https://announcements.bybit.com/static/announcements/_next/static/chunks/"
    "pages/article_filter/%5BfilterKeyword%5D-2ca5aa1a28ef5130.js"
)
SEARCH_BUILD_MANIFEST_SHA256 = "ccec1c6aaf8d6be1a14a5c5ac709dce13f3f7fa9d37ef86e7a72c93590020a10"
SEARCH_ARTICLE_FILTER_CHUNK_SHA256 = "2c5b9a62be8d4b4c173d4e4bc817595bbb3b0f49e6b10cd98938efa5fec9b3d7"
REGISTERED_KLAY_EFFECTIVE_MS = 1_730_084_400_000
REGISTERED_KLAY_UID = "blt5ff8d91e5ecd3d34"
ARTICLE_RE = re.compile(
    r"https://announcements\.bybit\.com/en/article/[^\"'<>\\]+/",
    re.IGNORECASE,
)
NEXT_DATA_RE = re.compile(
    rb'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)
MONTH_PATTERN = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?"
)
MONTH_TIME_RE = re.compile(
    rf"(?P<month>{MONTH_PATTERN})\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*,\s*"
    r"(?P<year>20\d{2})(?:\s*,|\s+at)?\s*"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<ampm>AM|PM)?\s*(?:\(UTC\)|UTC)",
    re.IGNORECASE,
)
ISO_TIME_RE = re.compile(
    r"(?P<year>20\d{2})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"(?:T|\s+)(?P<hour>\d{2}):(?P<minute>\d{2})"
    r"(?::\d{2})?\s*(?:Z|UTC)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ArticleEvidence:
    url: str
    uid: str
    title: str
    description: str
    published_at: str
    published_ts_ms: int
    paragraphs: tuple[str, ...]
    html_sha256: str
    raw_relative_path: str


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_bytes_create(path: Path, data: bytes) -> None:
    if path.exists():
        raise FileExistsError(f"create-only lifecycle artifact exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        if temporary.write_bytes(data) != len(data):
            raise OSError(f"short lifecycle write: {temporary}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_create(path: Path, value: Any) -> None:
    _write_bytes_create(path, _canonical_json(value) + b"\n")


def _write_parquet_create(path: Path, frame: pl.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(f"create-only lifecycle artifact exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.write_parquet(temporary, compression="zstd", statistics=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fetch(url: str, *, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001 - preserve source failures in census
            last_error = exc
            if attempt < attempts:
                time.sleep(0.25 * attempt)
    assert last_error is not None
    raise last_error


def _search_session() -> tuple[urllib.request.OpenerDirector, bytes]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    request = urllib.request.Request(
        SEARCH_BOOTSTRAP_URL,
        headers={"User-Agent": SEARCH_USER_AGENT, "Accept": "text/html"},
    )
    with opener.open(request, timeout=45) as response:
        raw = response.read()
    if not raw or not tuple(jar):
        raise RuntimeError("official search bootstrap did not establish a session")
    return opener, raw


def _search_request_body(symbol: str, page: int) -> dict[str, Any]:
    return {"query": symbol, "page": page, "hitsPerPage": 50}


def _search_post(
    opener: urllib.request.OpenerDirector,
    *,
    symbol: str,
    page: int,
    attempts: int = 3,
) -> tuple[bytes, bytes]:
    body = _canonical_json(_search_request_body(symbol, page))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            SEARCH_ENDPOINT,
            data=body,
            headers={
                "User-Agent": SEARCH_USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://announcements.bybit.com",
                "Referer": (
                    f"https://announcements.bybit.com/en/article_filter/{urllib.parse.quote(symbol, safe='')}/"
                ),
            },
        )
        try:
            with opener.open(request, timeout=45) as response:
                return body, response.read()
        except Exception as exc:  # noqa: BLE001 - freeze source failures
            last_error = exc
            if attempt < attempts:
                time.sleep(0.25 * attempt)
    assert last_error is not None
    raise last_error


def _search_result(
    raw: bytes,
    *,
    symbol: str,
    page: int,
) -> tuple[int, tuple[Mapping[str, Any], ...]]:
    payload = json.loads(raw)
    if payload.get("ret_code") != 0:
        raise ValueError(f"official search rejected {symbol} page {page}: {payload.get('ret_msg')}")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise ValueError("official search result is not an object")
    if str(result.get("query") or "") != symbol:
        raise ValueError("official search echoed the wrong symbol query")
    if int(result.get("page", -1)) != page:
        raise ValueError("official search echoed the wrong page")
    if int(result.get("hitsPerPage", -1)) != 50:
        raise ValueError("official search changed the registered page size")
    total = int(result.get("nbHits", -1))
    if total < 0:
        raise ValueError("official search returned an invalid hit count")
    hits = result.get("hits")
    if not isinstance(hits, list) or not all(isinstance(hit, Mapping) for hit in hits):
        raise ValueError("official search hits are not an object list")
    expected = min(50, max(total - page * 50, 0))
    if len(hits) != expected:
        raise ValueError(f"official search page coverage changed: {len(hits)} != {expected}")
    return total, tuple(hits)


def _official_article_url(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("/article/"):
        raw = f"https://announcements.bybit.com/en{raw}"
    parsed = urllib.parse.urlparse(raw)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "announcements.bybit.com"
        or not parsed.path.startswith("/en/article/")
    ):
        raise ValueError(f"search hit has a nonofficial article URL: {raw!r}")
    return urllib.parse.urlunparse(("https", "announcements.bybit.com", parsed.path, "", "", ""))


def _stable_search_hit(hit: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only query-specific display decoration from a frozen hit."""

    return {key: value for key, value in hit.items() if key != "_highlightResult"}


def _next_data(raw_html: bytes) -> Mapping[str, Any]:
    match = NEXT_DATA_RE.search(raw_html)
    if match is None:
        raise ValueError("official announcement page lacks __NEXT_DATA__")
    value = json.loads(html.unescape(match.group(1).decode("utf-8")))
    if not isinstance(value, Mapping):
        raise ValueError("official announcement __NEXT_DATA__ is not an object")
    return value


def _text_nodes(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            yield text.strip()
        for key, child in value.items():
            if key != "text":
                yield from _text_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _text_nodes(child)


def _published_ts_ms(value: str) -> int:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("announcement publication time lacks a timezone")
    return int(parsed.timestamp() * 1000)


def _parse_article(url: str, raw_html: bytes, raw_relative_path: str) -> ArticleEvidence:
    data = _next_data(raw_html)
    detail = data["props"]["pageProps"]["articleDetail"]
    content = detail.get("content", {}).get("json", {})
    paragraphs = tuple(dict.fromkeys(_text_nodes(content)))
    published_at = str(detail["date"])
    uid = str(data["props"]["pageProps"].get("articleUID") or "")
    if not uid:
        raise ValueError("official announcement page lacks an article UID")
    return ArticleEvidence(
        url=url,
        uid=uid,
        title=str(detail.get("title") or ""),
        description=str(detail.get("description") or ""),
        published_at=published_at,
        published_ts_ms=_published_ts_ms(published_at),
        paragraphs=paragraphs,
        html_sha256=_sha256_bytes(raw_html),
        raw_relative_path=raw_relative_path,
    )


def _month_number(value: str) -> int:
    normalized = value[:3].lower()
    names = {
        "jan": 1,
        "feb": 2,
        "mar": 3,
        "apr": 4,
        "may": 5,
        "jun": 6,
        "jul": 7,
        "aug": 8,
        "sep": 9,
        "oct": 10,
        "nov": 11,
        "dec": 12,
    }
    return names[normalized]


def _effective_times_ms(text: str) -> tuple[int, ...]:
    output: set[int] = set()
    for match in MONTH_TIME_RE.finditer(text):
        hour = int(match.group("hour"))
        ampm = (match.group("ampm") or "").upper()
        if ampm:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if ampm == "PM" else 0)
        if not 0 <= hour <= 23:
            continue
        value = dt.datetime(
            int(match.group("year")),
            _month_number(match.group("month")),
            int(match.group("day")),
            hour,
            int(match.group("minute") or 0),
            tzinfo=dt.timezone.utc,
        )
        output.add(int(value.timestamp() * 1000))
    for match in ISO_TIME_RE.finditer(text):
        value = dt.datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            tzinfo=dt.timezone.utc,
        )
        output.add(int(value.timestamp() * 1000))
    return tuple(sorted(output))


def _manifest_spans(root: Path) -> pl.DataFrame:
    paths = sorted(root.glob("date=*/part.parquet"))
    if not paths:
        raise FileNotFoundError(f"no direct manifest parts found under {root}")
    frame = (
        pl.scan_parquet([str(path) for path in paths])
        .group_by("symbol")
        .agg(
            pl.col("date").min().alias("first_date"),
            pl.col("date").max().alias("last_date"),
            pl.len().alias("direct_days"),
        )
        .collect()
        .with_columns((pl.col("last_date") < REGISTERED_END_DATE).alias("terminal_before_end"))
        .sort("symbol")
    )
    if frame.height != 903:
        raise RuntimeError(f"registered manifest symbol count changed: {frame.height} != 903")
    terminal = frame.filter(pl.col("terminal_before_end"))
    if terminal.height != 286:
        raise RuntimeError(f"registered terminal symbol count changed: {terminal.height} != 286")
    return frame


def _candidate_url(url: str, terminal_symbols: Sequence[str]) -> bool:
    slug = urllib.parse.urlparse(url).path.lower()
    if "delist" in slug:
        return True
    segments = set(re.split(r"[^a-z0-9]+", slug))
    for symbol in terminal_symbols:
        base = symbol.removesuffix("USDT").lower()
        if base and (base in segments or (len(base) >= 4 and base in slug)):
            return True
    return False


def _article_symbols(article: ArticleEvidence, symbols: Sequence[str]) -> tuple[str, ...]:
    text = "\n".join((article.title, article.description, *article.paragraphs)).upper()
    return tuple(symbol for symbol in symbols if re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", text))


def _article_effective_time(
    article: ArticleEvidence,
    symbol: str,
) -> tuple[int | None, str]:
    relevant = [
        value
        for value in (article.description, *article.paragraphs)
        if symbol.lower() in value.lower() and "utc" in value.lower()
    ]
    times = set(value for paragraph in relevant for value in _effective_times_ms(paragraph))
    if not times:
        all_text = "\n".join((article.description, *article.paragraphs))
        times.update(_effective_times_ms(all_text))
    if len(times) != 1:
        return None, f"effective_time_count={len(times)}"
    return next(iter(times)), ""


def _admission_reason(
    article: ArticleEvidence,
    symbol: str,
    effective_ts_ms: int | None,
) -> str:
    text = " ".join((article.title, article.description, *article.paragraphs)).lower()
    failures: list[str] = []
    if "perpetual contract" not in text:
        failures.append("not_perpetual_contract")
    if not ("open positions" in text and "automatically closed" in text):
        failures.append("automatic_close_not_explicit")
    if not ("average index price" in text and "30 minutes" in text):
        failures.append("settlement_reference_not_explicit")
    if effective_ts_ms is None:
        failures.append("effective_time_not_unique")
    elif not article.published_ts_ms < effective_ts_ms < REGISTERED_END_MS:
        failures.append("event_clock_out_of_scope")
    if symbol not in text.upper():
        failures.append("symbol_not_explicit")
    return ",".join(failures)


def _index_url(symbol: str, effective_ts_ms: int) -> str:
    query = urllib.parse.urlencode(
        {
            "category": "linear",
            "symbol": symbol,
            "interval": "1",
            "start": effective_ts_ms - 30 * 60_000,
            "end": effective_ts_ms - 60_000,
            "limit": 1000,
        }
    )
    return f"https://api.bybit.com/v5/market/index-price-kline?{query}"


def _index_proxy(
    symbol: str,
    effective_ts_ms: int,
    raw: bytes,
) -> tuple[float, str, list[dict[str, Any]]]:
    payload = json.loads(raw)
    if payload.get("retCode") != 0:
        raise ValueError(f"Bybit index API rejected request: {payload.get('retMsg')}")
    result = payload.get("result") or {}
    if str(result.get("symbol") or "").upper() != symbol:
        raise ValueError("Bybit index API returned the wrong symbol")
    rows: dict[int, Decimal] = {}
    canonical: list[dict[str, Any]] = []
    for item in result.get("list") or []:
        timestamp = int(item[0])
        close = Decimal(str(item[4]))
        if timestamp in rows:
            raise ValueError("Bybit index API returned duplicate minutes")
        if not close.is_finite() or close <= 0:
            raise ValueError("Bybit index API returned a nonpositive close")
        rows[timestamp] = close
    expected = tuple(range(effective_ts_ms - 30 * 60_000, effective_ts_ms, 60_000))
    if tuple(sorted(rows)) != expected:
        raise ValueError(f"Bybit index API minute coverage changed: {len(rows)} rows")
    for timestamp in expected:
        canonical.append({"ts_ms": timestamp, "close": str(rows[timestamp])})
    proxy = sum((rows[timestamp] for timestamp in expected), Decimal(0)) / Decimal(30)
    proxy_float = float(proxy)
    if not math.isfinite(proxy_float) or proxy_float <= 0.0:
        raise ValueError("Bybit index proxy is not positive and finite")
    return proxy_float, format(proxy, "f"), canonical


def _artifact_identities(root: Path) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for path in sorted(value for value in root.rglob("*") if value.is_file()):
        if path.name == "receipt.json":
            continue
        output[path.relative_to(root).as_posix()] = {
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-root", type=Path, default=DEFAULT_MANIFEST_ROOT)
    parser.add_argument("--linkmap-root", type=Path, default=DEFAULT_LINKMAP_ROOT)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--workers", type=int, default=8)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.workers <= 16:
        raise ValueError("lifecycle census workers must be between 1 and 16")
    manifest_root = args.manifest_root.expanduser().resolve(strict=True)
    linkmap_root = args.linkmap_root.expanduser().resolve(strict=True)
    output = args.out.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"create-only lifecycle output exists: {output}")
    work = output.with_name(f".{output.name}.working")
    if work.exists():
        raise FileExistsError(f"preserved lifecycle attempt exists: {work}")
    work.mkdir(parents=True)
    retrieved_at = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    spans = _manifest_spans(manifest_root)
    _write_parquet_create(work / "manifest_symbol_spans.parquet", spans)
    terminal_symbols = tuple(spans.filter(pl.col("terminal_before_end"))["symbol"].to_list())

    article_urls: set[str] = set()
    for page in range(1, REGISTERED_LINKMAP_PAGES + 1):
        source = linkmap_root / f"page-{page:02d}.html"
        raw = source.read_bytes()
        _write_bytes_create(work / f"source/linkmap/page-{page:02d}.html", raw)
        data = _next_data(raw)
        total_pages = int(data["props"]["pageProps"]["totalPages"])
        if total_pages != REGISTERED_LINKMAP_PAGES:
            raise RuntimeError(f"Bybit linkmap page count changed: {total_pages} != 12")
        decoded = html.unescape(raw.decode("utf-8", "replace"))
        article_urls.update(ARTICLE_RE.findall(decoded))
    english_urls = tuple(sorted(article_urls))

    build_manifest = _fetch(SEARCH_BUILD_MANIFEST_URL)
    article_filter_chunk = _fetch(SEARCH_ARTICLE_FILTER_CHUNK_URL)
    if _sha256_bytes(build_manifest) != SEARCH_BUILD_MANIFEST_SHA256:
        raise RuntimeError("registered search build-manifest identity changed")
    if _sha256_bytes(article_filter_chunk) != SEARCH_ARTICLE_FILTER_CHUNK_SHA256:
        raise RuntimeError("registered search client identity changed")
    _write_bytes_create(
        work / "source/search_client/build-manifest.js",
        build_manifest,
    )
    _write_bytes_create(
        work / "source/search_client/article-filter.js",
        article_filter_chunk,
    )

    search_opener, bootstrap_html = _search_session()
    _write_bytes_create(
        work / "source/search_client/bootstrap.html",
        bootstrap_html,
    )
    search_urls: set[str] = set()
    search_rows: list[dict[str, Any]] = []
    object_payloads: dict[str, bytes] = {}
    search_total_hits = 0
    for symbol in terminal_symbols:
        page = 0
        expected_total: int | None = None
        collected = 0
        while expected_total is None or collected < expected_total:
            request_body, raw = _search_post(
                search_opener,
                symbol=symbol,
                page=page,
            )
            relative_root = f"source/search/{symbol}/page-{page:03d}"
            _write_bytes_create(work / f"{relative_root}.request.json", request_body + b"\n")
            _write_bytes_create(work / f"{relative_root}.response.json", raw)
            total, hits = _search_result(raw, symbol=symbol, page=page)
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise RuntimeError(f"official search hit count changed during {symbol} pagination")
            for hit in hits:
                object_id = str(hit.get("objectID") or "")
                if not object_id:
                    raise ValueError("official search hit lacks an object identity")
                canonical_hit = _canonical_json(_stable_search_hit(hit))
                previous = object_payloads.get(object_id)
                if previous is not None and previous != canonical_hit:
                    raise RuntimeError(f"official search object changed during census: {object_id}")
                object_payloads[object_id] = canonical_hit
                search_urls.add(_official_article_url(hit.get("url")))
            collected += len(hits)
            search_rows.append(
                {
                    "symbol": symbol,
                    "page": page,
                    "reported_hits": total,
                    "page_hits": len(hits),
                    "request_sha256": _sha256_bytes(request_body),
                    "response_sha256": _sha256_bytes(raw),
                }
            )
            page += 1
        if expected_total is None or collected != expected_total:
            raise RuntimeError(f"official search pagination is incomplete for {symbol}")
        search_total_hits += collected

    search_frame = pl.from_dicts(
        search_rows,
        schema={
            "symbol": pl.String,
            "page": pl.Int64,
            "reported_hits": pl.Int64,
            "page_hits": pl.Int64,
            "request_sha256": pl.String,
            "response_sha256": pl.String,
        },
    ).sort(["symbol", "page"])
    _write_parquet_create(work / "search_queries.parquet", search_frame)

    candidate_urls = tuple(sorted(set(english_urls) | search_urls))

    fetched: dict[str, bytes] = {}
    fetch_errors: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        article_futures = {pool.submit(_fetch, url): url for url in candidate_urls}
        for future in as_completed(article_futures):
            url = article_futures[future]
            try:
                fetched[url] = future.result()
            except Exception as exc:  # noqa: BLE001 - evidence retains all failures
                fetch_errors.append({"url": url, "stage": "article_fetch", "error": repr(exc)})

    articles: list[ArticleEvidence] = []
    parse_errors: list[dict[str, Any]] = []
    for url, raw in sorted(fetched.items()):
        slug = urllib.parse.urlparse(url).path.rstrip("/").split("/")[-1]
        relative = f"source/articles/{slug}.html"
        _write_bytes_create(work / relative, raw)
        try:
            articles.append(_parse_article(url, raw, relative))
        except Exception as exc:  # noqa: BLE001 - evidence retains all failures
            parse_errors.append({"url": url, "stage": "article_parse", "error": repr(exc)})

    critical_article_failures = sorted(
        {str(row["url"]) for row in (*fetch_errors, *parse_errors) if str(row.get("url") or "") in search_urls}
    )

    span_by_symbol = {str(row["symbol"]): row for row in spans.to_dicts()}
    candidates: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = [*fetch_errors, *parse_errors]
    for article in articles:
        symbols = _article_symbols(article, terminal_symbols)
        for symbol in symbols:
            effective_ts_ms, time_error = _article_effective_time(article, symbol)
            reason = _admission_reason(article, symbol, effective_ts_ms)
            if time_error and "effective_time_not_unique" not in reason:
                reason = ",".join(filter(None, (reason, time_error)))
            row = {
                **asdict(article),
                "paragraphs": json.dumps(article.paragraphs, ensure_ascii=False),
                "symbol": symbol,
                "first_manifest_date": span_by_symbol[symbol]["first_date"],
                "last_manifest_date": span_by_symbol[symbol]["last_date"],
                "effective_ts_ms": effective_ts_ms,
                "admission_reason": reason,
            }
            if reason:
                unresolved.append({**row, "stage": "article_admission", "error": reason})
            else:
                candidates.append(row)

    deduped: dict[tuple[str, int], dict[str, Any]] = {}
    duplicate_event_count = 0
    for row in sorted(candidates, key=lambda value: (value["symbol"], value["effective_ts_ms"], value["url"])):
        key = (str(row["symbol"]), int(row["effective_ts_ms"]))
        if key in deduped:
            duplicate_event_count += 1
            unresolved.append(
                {
                    **row,
                    "stage": "article_admission",
                    "error": "duplicate_official_event",
                }
            )
            continue
        deduped[key] = row

    index_results: dict[tuple[str, int], tuple[bytes, str]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        index_futures = {
            pool.submit(_fetch, _index_url(symbol, effective_ts_ms)): (
                symbol,
                effective_ts_ms,
                _index_url(symbol, effective_ts_ms),
            )
            for symbol, effective_ts_ms in deduped
        }
        for future in as_completed(index_futures):
            symbol, effective_ts_ms, url = index_futures[future]
            try:
                index_results[(symbol, effective_ts_ms)] = (future.result(), url)
            except Exception as exc:  # noqa: BLE001 - evidence retains all failures
                unresolved.append(
                    {
                        "symbol": symbol,
                        "effective_ts_ms": effective_ts_ms,
                        "stage": "index_fetch",
                        "url": url,
                        "error": repr(exc),
                    }
                )

    events: list[dict[str, Any]] = []
    index_failure_count = 0
    for key, row in sorted(deduped.items()):
        result = index_results.get(key)
        if result is None:
            index_failure_count += 1
            continue
        raw, request_url = result
        symbol, effective_ts_ms = key
        stem = f"{symbol}-{effective_ts_ms}"
        raw_relative = f"source/index_price/{stem}.raw.json"
        _write_bytes_create(work / raw_relative, raw)
        try:
            proxy_price, proxy_decimal, canonical = _index_proxy(
                symbol,
                effective_ts_ms,
                raw,
            )
        except Exception as exc:  # noqa: BLE001 - evidence retains all failures
            index_failure_count += 1
            unresolved.append(
                {
                    **row,
                    "stage": "index_validation",
                    "url": request_url,
                    "error": repr(exc),
                    "index_api_sha256": _sha256_bytes(raw),
                }
            )
            continue
        canonical_relative = f"source/index_price/{stem}.canonical.json"
        _write_json_create(
            work / canonical_relative,
            {
                "symbol": symbol,
                "effective_ts_ms": effective_ts_ms,
                "request_url": request_url,
                "minute_closes": canonical,
                "proxy_decimal": proxy_decimal,
                "proxy_method": DELISTING_PROXY_METHOD,
                "proxy_exactness": DELISTING_PROXY_EXACTNESS,
            },
        )
        dispatch_ts_ms = ((effective_ts_ms + MS_PER_HOUR - 1) // MS_PER_HOUR) * MS_PER_HOUR
        events.append(
            {
                "symbol": symbol,
                "effective_ts_ms": effective_ts_ms,
                "dispatch_ts_ms": dispatch_ts_ms,
                "proxy_price": proxy_price,
                "proxy_price_decimal": proxy_decimal,
                "announcement_published_ts_ms": int(row["published_ts_ms"]),
                "announcement_url": row["url"],
                "announcement_uid": row["uid"],
                "announcement_sha256": row["html_sha256"],
                "index_api_sha256": _sha256_bytes(raw),
                "index_api_canonical_sha256": _sha256(work / canonical_relative),
                "proxy_method": DELISTING_PROXY_METHOD,
                "proxy_exactness": DELISTING_PROXY_EXACTNESS,
                "settlement_fee_usdt": 0.0,
                "source_scope": "official_bybit_announcement_and_index_price_api",
            }
        )

    admitted_symbols = {str(row["symbol"]) for row in events}
    reported_hits_by_symbol = {
        str(row["symbol"]): int(row["reported_hits"]) for row in search_rows if int(row["page"]) == 0
    }
    for symbol in terminal_symbols:
        if symbol in admitted_symbols:
            continue
        unresolved.append(
            {
                "symbol": symbol,
                "first_manifest_date": span_by_symbol[symbol]["first_date"],
                "last_manifest_date": span_by_symbol[symbol]["last_date"],
                "stage": "symbol_resolution",
                "error": "no_admissible_official_event_after_complete_search",
                "search_reported_hits": reported_hits_by_symbol[symbol],
            }
        )

    event_schema = {
        "symbol": pl.String,
        "effective_ts_ms": pl.Int64,
        "dispatch_ts_ms": pl.Int64,
        "proxy_price": pl.Float64,
        "proxy_price_decimal": pl.String,
        "announcement_published_ts_ms": pl.Int64,
        "announcement_url": pl.String,
        "announcement_uid": pl.String,
        "announcement_sha256": pl.String,
        "index_api_sha256": pl.String,
        "index_api_canonical_sha256": pl.String,
        "proxy_method": pl.String,
        "proxy_exactness": pl.String,
        "settlement_fee_usdt": pl.Float64,
        "source_scope": pl.String,
    }
    event_frame = pl.from_dicts(events, schema=event_schema).sort(["dispatch_ts_ms", "effective_ts_ms", "symbol"])
    klay_rows = [
        row
        for row in events
        if row["symbol"] == "KLAYUSDT"
        and row["effective_ts_ms"] == REGISTERED_KLAY_EFFECTIVE_MS
        and row["announcement_uid"] == REGISTERED_KLAY_UID
    ]
    search_queries_completed = sum(1 for row in search_rows if int(row["page"]) == 0)
    coverage_errors: list[str] = []
    if search_queries_completed != len(terminal_symbols):
        coverage_errors.append("terminal_search_query_count_changed")
    if critical_article_failures:
        coverage_errors.append("search_result_article_fetch_or_parse_failure")
    if duplicate_event_count:
        coverage_errors.append("duplicate_admissible_event")
    if index_failure_count:
        coverage_errors.append("admissible_event_index_input_failure")
    if len(klay_rows) != 1:
        coverage_errors.append("registered_klay_event_missing_or_ambiguous")
    coverage_valid = not coverage_errors
    unresolved_frame = (
        pl.from_dicts(
            [
                {
                    key: (
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        if isinstance(value, (Mapping, list, tuple))
                        else value
                    )
                    for key, value in row.items()
                }
                for row in unresolved
            ],
            infer_schema_length=None,
        )
        if unresolved
        else pl.DataFrame({"stage": pl.Series([], dtype=pl.String)})
    )
    _write_parquet_create(work / "events.parquet", event_frame)
    _write_parquet_create(work / "unresolved.parquet", unresolved_frame)
    _write_json_create(
        work / "discovery.json",
        {
            "retrieved_at": retrieved_at,
            "registered_linkmap_pages": REGISTERED_LINKMAP_PAGES,
            "english_article_urls": len(english_urls),
            "search_queries_completed": search_queries_completed,
            "search_total_hits": search_total_hits,
            "search_article_urls": len(search_urls),
            "candidate_article_urls": len(candidate_urls),
            "fetched_articles": len(fetched),
            "parsed_articles": len(articles),
            "manifest_symbols": spans.height,
            "terminal_manifest_symbols": len(terminal_symbols),
            "admitted_events": event_frame.height,
            "unresolved_rows": unresolved_frame.height,
            "coverage_valid": coverage_valid,
            "coverage_errors": coverage_errors,
        },
    )

    files = _artifact_identities(work)
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "status": "pass" if coverage_valid else "fail",
        "kind": "official_bybit_venue_lifecycle_census",
        "retrieved_at": retrieved_at,
        "manifest_root": str(manifest_root),
        "registered_end_date": REGISTERED_END_DATE,
        "registered_linkmap_pages": REGISTERED_LINKMAP_PAGES,
        "frozen_linkmap_root": str(linkmap_root),
        "search_build_id": SEARCH_BUILD_ID,
        "search_endpoint": SEARCH_ENDPOINT,
        "search_queries_completed": search_queries_completed,
        "search_total_hits": search_total_hits,
        "search_article_urls": len(search_urls),
        "critical_article_failures": critical_article_failures,
        "duplicate_admissible_events": duplicate_event_count,
        "admissible_event_index_failures": index_failure_count,
        "registered_klay_event_count": len(klay_rows),
        "coverage_valid": coverage_valid,
        "coverage_errors": coverage_errors,
        "manifest_symbols": spans.height,
        "terminal_manifest_symbols": len(terminal_symbols),
        "english_article_urls": len(english_urls),
        "candidate_article_urls": len(candidate_urls),
        "fetched_articles": len(fetched),
        "parsed_articles": len(articles),
        "admitted_events": event_frame.height,
        "unresolved_rows": unresolved_frame.height,
        "proxy_method": DELISTING_PROXY_METHOD,
        "proxy_exactness": DELISTING_PROXY_EXACTNESS,
        "monetary_outcomes_inspected": False,
        "files": files,
        "explicit_non_conclusions": [
            "no exact venue per-second settlement-price claim",
            "no P&L, fee, funding, cost, or TCA claim",
            "no inference that an unmatched terminal symbol was delisted",
            "no strategy, alpha, thesis, deployment, or real-money authority",
        ],
    }
    receipt["receipt_payload_sha256"] = payload_sha256(receipt)
    _write_json_create(work / "receipt.json", receipt)
    os.replace(work, output)
    print(
        json.dumps(
            {
                "output": str(output),
                "status": receipt["status"],
                "receipt_sha256": _sha256(output / "receipt.json"),
                "admitted_events": event_frame.height,
                "unresolved_rows": unresolved_frame.height,
                "search_queries_completed": search_queries_completed,
                "coverage_valid": coverage_valid,
                "coverage_errors": coverage_errors,
                "monetary_outcomes_inspected": False,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
