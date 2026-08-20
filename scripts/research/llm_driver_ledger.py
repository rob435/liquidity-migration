"""Forward-only ledger for the mover-driver judgment (the LLM discriminator).

Every mechanical "buy strength" rework of the LONG sleeve has been measured
dead on this book, and the one decomposition that survives says: the timing
edge on a pump is real only when the pump goes on to confirm, and whether it
confirms is not in the price/turnover panel. A language model judging the
DRIVER (a listing, real news, a shill, or plain market beta) can only be
graded honestly on calls it makes in advance — its training data already
knows how every historical pump ended.

So this script does exactly two things, and never trades:

  --once   nominate current movers from Bybit public tickers (loose quant
           nominator), ask the model to judge each driver, and append the
           facts + judgment to a JSONL ledger, timestamped, before the
           outcome exists.
  --grade  for ledger rows at least 3 days old, fetch what actually happened
           (public klines) and print the discriminator table: forward return
           by judged driver kind.

Environment: DEEPSEEK_API_KEY (or LLM_API_KEY) arms the judgment call;
LLM_BASE_URL (default https://api.deepseek.com) and LLM_MODEL (default
deepseek-chat) select the endpoint — any OpenAI-compatible chat API works.
Without a key, --once records the nominations with judgment=null so the
nomination clock keeps running.

Ledger: ~/SHARED_DATA/llm_driver_ledger/ledger.jsonl (override with
--ledger-dir). Public read-only market data; no account, no orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

BYBIT_PUBLIC = "https://api.bybit.com"
PROMPT_VERSION = "driver-judgment-v1"

# Loose nominator: enough movers to give the discriminator something to
# separate, few enough that every row gets judged.
MIN_MOVE_24H = 0.10
MAX_TURNOVER_RANK = 30
NOMINEES_MAX = 12


def _http_json(url: str, *, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=60) as resp:
        return json.loads(resp.read())


def nominate() -> list[dict[str, Any]]:
    body = _http_json(f"{BYBIT_PUBLIC}/v5/market/tickers?category=linear")
    rows = (body.get("result") or {}).get("list") or []
    usdt = [r for r in rows if str(r.get("symbol", "")).endswith("USDT")]
    for r in usdt:
        r["_turnover"] = float(r.get("turnover24h") or 0.0)
        r["_move"] = float(r.get("price24hPcnt") or 0.0)
    usdt.sort(key=lambda r: -r["_turnover"])
    nominees = []
    for rank, r in enumerate(usdt, start=1):
        if rank > MAX_TURNOVER_RANK:
            break
        if r["_move"] < MIN_MOVE_24H:
            continue
        high = float(r.get("highPrice24h") or 0.0)
        low = float(r.get("lowPrice24h") or 0.0)
        last = float(r.get("lastPrice") or 0.0)
        close_loc = (last - low) / (high - low) if high > low else 0.5
        nominees.append(
            {
                "symbol": r["symbol"],
                "move_24h": round(r["_move"], 4),
                "turnover_24h_usdt": round(r["_turnover"], 0),
                "turnover_rank": rank,
                "range_location": round(close_loc, 3),
                "last_price": last,
            }
        )
    return nominees[:NOMINEES_MAX]


def judge(facts: dict[str, Any]) -> dict[str, Any] | None:
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        return None
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    prompt = (
        "A crypto perpetual just moved. Facts: "
        + json.dumps(facts)
        + "\nJudge the DRIVER of this move from your knowledge of this token and"
        " how such moves usually resolve. Reply with ONLY a JSON object:"
        ' {"driver_kind": "listing|news|shill|fundamental|market_beta|unknown",'
        ' "idiosyncratic": true/false, "will_hold_24h": true/false,'
        ' "confidence": 0.0-1.0, "reason": "one sentence"}'
    )
    body = _http_json(
        f"{base}/chat/completions",
        payload={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    text = body["choices"][0]["message"]["content"]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {"parse_error": text[:200], "model": model}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {"parse_error": text[:200], "model": model}
    parsed["model"] = model
    return parsed


def cmd_once(ledger_dir: Path) -> None:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / "ledger.jsonl"
    now = dt.datetime.now(dt.timezone.utc)
    nominees = nominate()
    with path.open("a") as fh:
        for facts in nominees:
            row = {
                "ts_utc": now.isoformat(timespec="seconds"),
                "prompt_version": PROMPT_VERSION,
                "facts": facts,
                "judgment": judge(facts),
            }
            fh.write(json.dumps(row) + "\n")
            print(f"{facts['symbol']:<14} +{facts['move_24h']:.0%}  judged={bool(row['judgment'])}")
    print(f"{len(nominees)} nomination(s) appended to {path}")


def _forward_return(symbol: str, from_ms: int, hours: int) -> float | None:
    body = _http_json(
        f"{BYBIT_PUBLIC}/v5/market/kline?category=linear&symbol={symbol}"
        f"&interval=60&start={from_ms}&end={from_ms + (hours + 1) * 3_600_000}&limit=200"
    )
    rows = (body.get("result") or {}).get("list") or []
    if not rows:
        return None
    rows.sort(key=lambda r: int(r[0]))
    first_close = float(rows[0][4])
    target_ts = from_ms + hours * 3_600_000
    best = None
    for r in rows:
        if int(r[0]) <= target_ts:
            best = float(r[4])
    if best is None or first_close <= 0:
        return None
    return best / first_close - 1.0


def cmd_grade(ledger_dir: Path) -> None:
    path = ledger_dir / "ledger.jsonl"
    if not path.exists():
        sys.exit(f"no ledger at {path}")
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    buckets: dict[str, list[float]] = {}
    graded = 0
    for line in path.open():
        row = json.loads(line)
        ts = dt.datetime.fromisoformat(row["ts_utc"])
        if ts > cutoff:
            continue
        judgment = row.get("judgment") or {}
        kind = str(judgment.get("driver_kind", "unjudged"))
        fwd = _forward_return(row["facts"]["symbol"], int(ts.timestamp() * 1000), 72)
        if fwd is None:
            continue
        buckets.setdefault(kind, []).append(fwd)
        graded += 1
    print(f"graded {graded} row(s) at the 72h horizon:")
    for kind, vals in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        mean = sum(vals) / len(vals)
        wins = sum(1 for v in vals if v > 0) / len(vals)
        print(f"  {kind:<14} n={len(vals):<4} mean {mean:+.2%}  win {wins:.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="nominate and judge current movers")
    parser.add_argument("--grade", action="store_true", help="grade rows at least 3 days old")
    parser.add_argument(
        "--ledger-dir",
        default=str(Path.home() / "SHARED_DATA" / "llm_driver_ledger"),
        help="ledger directory (default ~/SHARED_DATA/llm_driver_ledger)",
    )
    args = parser.parse_args()
    ledger_dir = Path(args.ledger_dir).expanduser()
    if args.once:
        cmd_once(ledger_dir)
    elif args.grade:
        cmd_grade(ledger_dir)
    else:
        parser.error("pass --once or --grade")


if __name__ == "__main__":
    main()
