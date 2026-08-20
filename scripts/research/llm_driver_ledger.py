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
           nominator), enrich each with the public facts a judgment needs
           (funding, perp premium, open-interest change, beta context, range
           and volume anomaly, listing age), ask the model to walk a fixed
           methodology, and append facts + judgment to a JSONL ledger,
           timestamped, before the outcome exists.
  --grade  for ledger rows at least 3 days old, fetch what actually happened
           (public klines) and print forward return by prompt version and
           judged driver kind.

The methodology prompt is a rubric authored by the stronger model and
executed by the cheap one; each step reports its own field so a failed
forward grade can be localized to the step that failed. A rubric change is a
new prompt_version and grades separately.

Environment: DEEPSEEK_API_KEY (or LLM_API_KEY) arms the judgment call;
LLM_BASE_URL (default https://api.deepseek.com) and LLM_MODEL (default
deepseek-chat) select the endpoint — any OpenAI-compatible chat API works.
Without a key, --once still records fully-enriched nominations with
judgment=null so the nomination clock keeps running.

Ledger: ~/SHARED_DATA/llm_driver_ledger/ledger.jsonl (override with
--ledger-dir). Public read-only market data; no account, no orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import sys
import urllib.request
from pathlib import Path
from typing import Any

BYBIT_PUBLIC = "https://api.bybit.com"
PROMPT_VERSION = "driver-judgment-v3-scored"

# Loose nominator: enough movers to give the discriminator something to
# separate, few enough that every row gets judged.
MIN_MOVE_24H = 0.10
MAX_TURNOVER_RANK = 30
NOMINEES_MAX = 12

# The rubric the model executes. Authored once by the stronger model; the
# numbers in the priors step are this repo's own measurements
# (docs/research/archive/2026-08-21-long-v13-rework-program.md), not folklore.
METHODOLOGY = """You are judging one crypto perpetual pump for a systematic desk.
Walk these steps IN ORDER and report every step's answer in the JSON schema
below. Be concrete; a step you cannot ground must say so and lower the final
confidence.

STEP 1 — identity. From your knowledge of this token: what is it, roughly how
old and how large, does it have a real product or community, and how have its
past pumps resolved? If you do not recognize it, say "unknown token" — that
is itself informative (unknown microcaps pump on shills and listings).

STEP 2 — beta check. Compare the coin's 24h move to BTC's and ETH's in the
same window (provided). A move mostly explained by the market is not
idiosyncratic, whatever the headline number says. Use idio_move_24h as the
rough market-adjusted move.

STEP 3 — leverage vs organic. Read funding_rate, perp_premium_bp, and
oi_change_24h_pct together: premium and funding spiking positive with OI
sharply up means leverage is chasing (fragile, liquidation-prone in both
directions); flat premium with strong turnover and modest OI growth means
spot-led organic buying (stickier); OI DOWN on a pump means a short squeeze
(tends to fade once the shorts are cleared). Classify: leverage_chase |
spot_led | short_squeeze | mixed | unclear.

STEP 4 — structure. dist_from_30d_high_pct near zero means this pump is
breaking to new highs (a fresh move, or the exhaustion top of an old one —
use step 1 to tell which); a deep negative value means a bounce inside a
downtrend, which resolves worse. turnover_anomaly is today's volume against
the coin's own 90-day norm — above ~3 means genuinely anomalous attention.

STEP 5 — driver hypothesis. Combining steps 1–4 with your knowledge: the
most likely driver. You CANNOT see today's news or social feeds. If your
driver call is an inference from the token's identity and the tape rather
than an event you can actually name, set driver_grounded=false and cap
confidence at 0.6.

STEP 6 — priors (measured on this desk; override your instincts with them):
pumps at >=1.5x the coin's vol-adjusted bar confirmed their daily close 66%
of the time vs 33% below 1.2x (depth_ratio is provided); triggers after
12:00 UTC confirm materially better than 00-06 UTC; where price sits in the
24h range predicts nothing; and on these names crowding CONTINUES — "it is
up a lot so it must pull back" is measurably the wrong prior here. Do not be
contrarian by default.

STEP 7 — verdict. pump_quality_score is the headline: an integer 0-10 for
"how attractive is holding this pump from here for the next 1-3 days" —
0-2 avoid (beta, exhaustion, or squeeze already spent), 3-4 weak, 5-6
unclear, 7-8 good (idiosyncratic, flow supportive, structure fresh), 9-10
exceptional and rare. Also will_hold_24h (is the price more likely at or
above this level in 24h), confidence in [0,1], and one falsifiable sentence.
The score ranks pumps against each other; the desk sweeps thresholds on it
later, so use the full range and do not cluster at 5.

Reply with ONLY this JSON object, no other text:
{"identity": "one sentence", "recognized": true/false,
 "beta_share": "none|partial|mostly_market", "flow_type":
 "leverage_chase|spot_led|short_squeeze|mixed|unclear", "structure":
 "fresh_breakout|exhaustion_top|downtrend_bounce|range|unclear",
 "driver_kind": "listing|news|shill|fundamental|squeeze|market_beta|unknown",
 "driver_grounded": true/false, "idiosyncratic": true/false,
 "pump_quality_score": 0-10, "will_hold_24h": true/false,
 "confidence": 0.0-1.0, "reason": "one falsifiable sentence"}"""


def _http_json(url: str, *, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Any:
    req = urllib.request.Request(url, headers=headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=60) as resp:
        return json.loads(resp.read())


def _result_list(body: Any) -> list[Any]:
    return (body.get("result") or {}).get("list") or []


def enrich(symbol: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Attach the public facts the rubric consumes. Every field is optional:
    a failed read stays null and never blocks the nomination."""

    try:
        rows = _result_list(
            _http_json(
                f"{BYBIT_PUBLIC}/v5/market/kline?category=linear&symbol={symbol}&interval=D&limit=91"
            )
        )
        rows.sort(key=lambda r: int(r[0]))
        closes = [float(r[4]) for r in rows[:-1]]  # completed days only
        highs = [float(r[2]) for r in rows[:-1]]
        turnovers = [float(r[6]) for r in rows[:-1]]
        if len(closes) >= 31:
            rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 30, len(closes))]
            sigma = statistics.pstdev(rets)
            facts["sigma_daily_30d"] = round(sigma, 5)
            if sigma > 0:
                facts["depth_ratio"] = round(math.log1p(facts["move_24h"]) / (2.5 * sigma), 2)
        if highs:
            high_30d = max(highs[-30:])
            facts["dist_from_30d_high_pct"] = round((facts["last_price"] / high_30d - 1.0) * 100, 2)
        if turnovers:
            med = statistics.median(turnovers)
            if med > 0:
                facts["turnover_anomaly"] = round(facts["turnover_24h_usdt"] / med, 2)
    except Exception:
        pass
    try:
        oi = _result_list(
            _http_json(
                f"{BYBIT_PUBLIC}/v5/market/open-interest?category=linear&symbol={symbol}"
                "&intervalTime=1h&limit=25"
            )
        )
        if len(oi) >= 2:
            oi.sort(key=lambda r: int(r["timestamp"]))
            first, last = float(oi[0]["openInterest"]), float(oi[-1]["openInterest"])
            if first > 0:
                facts["oi_change_24h_pct"] = round((last / first - 1.0) * 100, 2)
    except Exception:
        pass
    try:
        info = _result_list(
            _http_json(f"{BYBIT_PUBLIC}/v5/market/instruments-info?category=linear&symbol={symbol}")
        )
        if info and info[0].get("launchTime"):
            age_days = (dt.datetime.now(dt.timezone.utc).timestamp() * 1000 - int(info[0]["launchTime"])) / 86_400_000
            facts["listing_age_days"] = int(age_days)
    except Exception:
        pass
    return facts


def nominate() -> list[dict[str, Any]]:
    body = _http_json(f"{BYBIT_PUBLIC}/v5/market/tickers?category=linear")
    rows = _result_list(body)
    usdt = [r for r in rows if str(r.get("symbol", "")).endswith("USDT")]
    by_symbol = {str(r["symbol"]): r for r in usdt}
    for r in usdt:
        r["_turnover"] = float(r.get("turnover24h") or 0.0)
        r["_move"] = float(r.get("price24hPcnt") or 0.0)
    usdt.sort(key=lambda r: -r["_turnover"])

    def _move(symbol: str) -> float | None:
        row = by_symbol.get(symbol)
        return round(float(row.get("price24hPcnt") or 0.0), 4) if row else None

    btc_move, eth_move = _move("BTCUSDT"), _move("ETHUSDT")
    nominees = []
    for rank, r in enumerate(usdt, start=1):
        if rank > MAX_TURNOVER_RANK:
            break
        if r["_move"] < MIN_MOVE_24H:
            continue
        high = float(r.get("highPrice24h") or 0.0)
        low = float(r.get("lowPrice24h") or 0.0)
        last = float(r.get("lastPrice") or 0.0)
        mark = float(r.get("markPrice") or 0.0)
        index = float(r.get("indexPrice") or 0.0)
        facts: dict[str, Any] = {
            "symbol": r["symbol"],
            "move_24h": round(r["_move"], 4),
            "turnover_24h_usdt": round(r["_turnover"], 0),
            "turnover_rank": rank,
            "range_location": round((last - low) / (high - low), 3) if high > low else 0.5,
            "last_price": last,
            "funding_rate": float(r.get("fundingRate") or 0.0),
            "perp_premium_bp": round((mark / index - 1.0) * 10_000, 1) if index > 0 else None,
            "btc_move_24h": btc_move,
            "eth_move_24h": eth_move,
            "hour_utc": dt.datetime.now(dt.timezone.utc).hour,
        }
        if btc_move is not None:
            facts["idio_move_24h"] = round(r["_move"] - btc_move, 4)
        nominees.append(enrich(str(r["symbol"]), facts))
    return nominees[:NOMINEES_MAX]


def judge(facts: dict[str, Any]) -> dict[str, Any] | None:
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY")
    if not key:
        return None
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    prompt = METHODOLOGY + "\n\nThe pump's facts (null = unavailable):\n" + json.dumps(facts, indent=1)
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


RENOMINATION_HOURS = 12


def _recently_nominated(path: Path, now: dt.datetime) -> set[str]:
    """Symbols already nominated inside the suppression window: one pump, one
    ledger row — hourly re-rows of the same move would only inflate n with
    correlated points."""

    if not path.exists():
        return set()
    cutoff = now - dt.timedelta(hours=RENOMINATION_HOURS)
    recent: set[str] = set()
    for line in path.open():
        try:
            row = json.loads(line)
            if dt.datetime.fromisoformat(row["ts_utc"]) >= cutoff:
                recent.add(str(row["facts"]["symbol"]))
        except Exception:
            continue
    return recent


def cmd_once(ledger_dir: Path) -> None:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / "ledger.jsonl"
    now = dt.datetime.now(dt.timezone.utc)
    recent = _recently_nominated(path, now)
    nominees = [f for f in nominate() if f["symbol"] not in recent]
    with path.open("a") as fh:
        for facts in nominees:
            judgment = judge(facts)
            row = {
                "ts_utc": now.isoformat(timespec="seconds"),
                "prompt_version": PROMPT_VERSION,
                "facts": facts,
                "judgment": judgment,
            }
            fh.write(json.dumps(row) + "\n")
            verdict = (
                f"score={judgment.get('pump_quality_score')} {judgment.get('driver_kind')}/"
                f"{'holds' if judgment.get('will_hold_24h') else 'fades'}@{judgment.get('confidence')}"
                if judgment
                else "unjudged"
            )
            print(f"{facts['symbol']:<14} +{facts['move_24h']:.0%}  {verdict}")
    print(f"{len(nominees)} nomination(s) appended to {path}")


def _forward_return(symbol: str, from_ms: int, hours: int) -> float | None:
    body = _http_json(
        f"{BYBIT_PUBLIC}/v5/market/kline?category=linear&symbol={symbol}"
        f"&interval=60&start={from_ms}&end={from_ms + (hours + 1) * 3_600_000}&limit=200"
    )
    rows = _result_list(body)
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
    buckets: dict[tuple[str, str], list[float]] = {}
    graded = 0
    for line in path.open():
        row = json.loads(line)
        ts = dt.datetime.fromisoformat(row["ts_utc"])
        if ts > cutoff:
            continue
        judgment = row.get("judgment") or {}
        kind = str(judgment.get("driver_kind", "unjudged"))
        version = str(row.get("prompt_version", "?"))
        fwd = _forward_return(row["facts"]["symbol"], int(ts.timestamp() * 1000), 72)
        if fwd is None:
            continue
        buckets.setdefault((version, kind), []).append(fwd)
        score = judgment.get("pump_quality_score")
        if isinstance(score, (int, float)):
            band = "score 0-3" if score <= 3 else ("score 4-6" if score <= 6 else "score 7-10")
            buckets.setdefault((version, band), []).append(fwd)
        graded += 1
    print(f"graded {graded} row(s) at the 72h horizon:")
    for (version, kind), vals in sorted(buckets.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        mean = sum(vals) / len(vals)
        wins = sum(1 for v in vals if v > 0) / len(vals)
        print(f"  {version:<26} {kind:<14} n={len(vals):<4} mean {mean:+.2%}  win {wins:.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="nominate, enrich, and judge current movers")
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
