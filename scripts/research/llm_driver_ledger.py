"""Forward-only ledger for the mover-driver judgment (the LLM discriminator).

Every mechanical "buy strength" rework of the LONG sleeve has been measured
dead on this book, and the one decomposition that survives says: the timing
edge on a pump is real only when the pump goes on to confirm, and whether it
confirms is not in the price/turnover panel. A language model judging the
DRIVER (a listing, real news, a shill, or plain market beta) can only be
graded honestly on calls it makes in advance — its training data already
knows how every historical pump ended.

So this script does exactly two things, and never trades:

  --once     nominate current movers from Bybit public tickers (loose quant
             nominator), enrich each with the public facts a judgment needs
             (funding, perp premium, open-interest change, volume against open
             interest, beta context, range and volume anomaly, listing age),
             ask the model to walk a fixed
             methodology, and append facts + judgment to a JSONL ledger,
             timestamped, before the outcome exists.
  --triggers run hourly: detect fresh intraday deep-trigger events (rolling
             24h window, the 2.5-sigma family, regime and ATR gates
             approximated from public data), judge each, journal the event,
             and publish every score >= 6 judgment to a research candidates
             file. It is not an input to the native LONG runtime and holds no
             venue credentials.
  --grade    for ledger rows at least 3 days old, fetch what actually happened
             (public klines) and print forward return by prompt version, row
             type, and judged driver kind.

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

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from liquidity_migration.core.durable_file import durable_atomic_replace  # noqa: E402

BYBIT_PUBLIC = "https://api.bybit.com"
#: Binance publishes who was aggressive; Bybit does not. Public, no key.
BINANCE_PUBLIC = "https://fapi.binance.com"
PROMPT_VERSION = "driver-judgment-v7-crime-pump"

# Loose nominator: enough movers to give the discriminator something to
# separate, few enough that every row gets judged.
MIN_MOVE_24H = 0.10
MAX_TURNOVER_RANK = 30
NOMINEES_MAX = 12

# How deep the ENTRY scan goes, which is not how deep the research scan goes.
# Turnover rank is the strongest single thing measured about these triggers:
# graded over 5.5 years, rank 1-5 earns 433 bp a trade, 1-10 earns 308, and
# the full 30 earns 154. The cut beats the wider one in every year. Thin books
# are where the fake pumps are, and no shape feature -- one-bar share, turnover
# spike, how often the name has fired before -- separated them.
TRIGGER_TURNOVER_RANK_MAX = 10

# The wide band (owner-directed forward A/B, change point 2026-08-30). Ranks
# 11-30 are scanned, judged, and published like the core band but the event
# carries band="wide", so the research cohorts grade apart. Mechanically the 11-30 pool
# is a lottery -- 9% of its triggers graduate to top-10 within 3 days
# (+1,805 bp/trade, 89% win) and the other 91% average -126 bp/trade
# (research_findings 2026-08-30) -- so what this band tests is precisely
# whether the judgment separates them. HNTUSDT 2026-08-30 is the motivating
# case: scored 7 at rank 11 at 01:05, enterable only at rank 9 at 09:05,
# +47% later.
TRIGGER_WIDE_RANK_MAX = 30

# The rubric the model executes. Every number in the priors step is this
# repo's own measurement, not folklore: the depth figure from the daily v13
# program, the rest graded on 5.5 years of these hourly triggers. The one
# outside number -- step 7's volume-to-open-interest band -- is labeled as
# unmeasured on this desk inside the prompt itself. Bump PROMPT_VERSION with
# any edit here -- `--grade` buckets by it.
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

STEP 3 — leverage vs organic. Read funding_rate, perp_premium_bp,
premium_change_24h_bp, oi_change_24h_pct and oi_change_48h_pct together and
classify: leverage_chase | spot_led | short_squeeze | mixed | unclear. Flat
premium with strong turnover and modest OI growth is spot-led organic buying.
OI DOWN on a pump is a short squeeze and tends to fade once the shorts are
cleared. But do NOT mark a pump down for open interest rising hard: measured
on this desk the fastest-growing OI quartile is the BEST of the four, so
"leverage is chasing, therefore fragile" is the wrong inference here even
though it is the usual one. The PATHS matter as much as the levels: a premium
rising into the pump (positive premium_change_24h_bp) is demand still
arriving; a collapsing premium at the same print is demand leaving. Use these
fields to classify the flow honestly — this desk has NOT measured a mechanical
edge in them, so they inform your classification, not the score directly.

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
of the time vs 33% below 1.2x (depth_ratio is provided);
taker_buy_sell_ratio_1d above about 1.07 marks a pump being lifted hardest at
the ask, and those work LESS OFTEN -- 41% of them ended up against 48% of the
quieter ones, median -207 bp against -38 bp, across five years. Treat a high
ratio as a reason for care rather than as confirmation. It is a caution and not
a rule: the quieter ones were better on average in four of the five years and
won more often in four of the five, but the exception years are different ones,
and in a hard melt-up the aggressively-bought names ran the furthest of all;
the hour of day
predicts nothing usable, so do not reason from it; sitting at the very top of
the range is slightly WORSE than sitting at three-quarters of it; and on these
names crowding CONTINUES — "it is up a lot so it must pull back" is measurably
the wrong prior here. Do not be contrarian by default. A deeper move is worth
more on average and wins less often: the edge is in the tail, so do not mark a
pump down for having run far.

STEP 7 — scam pump. Some of these are manufactured, and the manufactured
ones follow two documented shapes. (a) The low-float walk-up: a young listing
with most of its supply still locked, walked up by a coordinated cluster of
wallets on a book so thin that a few million dollars is the whole market —
the whole move from launch to top runs in days. (b) The squeeze bait: a pump
engineered to look obviously unsustainable so shorts crowd in — funding goes
deeply negative while price holds a range — and the operators then force the
shorts out in a liquidation cascade before dumping. The tape alone cannot
prove either — every mechanical shape measure this desk tested against
outcomes failed to separate a manufactured pump from a real one — so weigh
the provided facts together with what you know about the token itself. Ask:
is this a name with real usage and a real holder base, or a vehicle? Has it
done this before and given it all back? Is the 24h turnover large in
absolute terms, or is a big percentage move sitting on a small book? Is the
listing fresh (listing_age_days), and if you know its tokenomics, is most of
the supply still locked? And read turnover_to_oi_24h — the day's traded
volume against the standing open interest: outside research on manufactured
pumps reports low single digits as typical and reads sustained 20+ as
churned, self-traded volume. This desk has NOT measured that band; treat it
as one caution among several, never a rule. Set scam_pump_risk to
low | medium | high and manipulation_shape to
none | low_float_walk | squeeze_bait | unclear, and say in scam_pump_reason
which consideration decided it. High risk means the score must be 3 or below
whatever else looks good.

STEP 8 — verdict. pump_quality_score is the headline: an integer 0-10 for
"how attractive is holding this pump from here for the next 1-3 days" —
0-2 avoid (beta, exhaustion, or squeeze already spent), 3-4 weak, 5-6
unclear, 7-8 good (idiosyncratic, flow supportive, structure fresh), 9-10
exceptional and rare. Also will_hold_24h (is the price more likely at or
above this level in 24h), confidence in [0,1], and one falsifiable sentence.
The score ranks pumps against each other; the desk sweeps thresholds on it
later, so use the full range and do not cluster at 5.

Reply with ONLY this JSON object, no other text:
{"identity": "one sentence", "recognized": true/false,
 "scam_pump_risk": "low|medium|high", "scam_pump_reason": "one sentence",
 "manipulation_shape": "none|low_float_walk|squeeze_bait|unclear",
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


def taker_ratio_day_mean(rows: list[dict[str, Any]], midnight_ms: int) -> float | None:
    """Mean of the five-minute taker buy/sell ratios over the last completed day.

    The MEAN of ratios, not the ratio of the day's sums -- those are different
    numbers and the mean sits above the aggregate, so the threshold the rubric
    quotes is only meaningful against this one. A day the venue served in part
    is refused rather than averaged: a handful of buckets is not a day.
    """

    day = [
        float(row["buySellRatio"])
        for row in rows
        if midnight_ms - 86_400_000 <= int(row["timestamp"]) < midnight_ms
    ]
    if len(day) < 200:
        return None
    return round(statistics.fmean(day), 3)


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
        lows = [float(r[3]) for r in rows[:-1]]
        turnovers = [float(r[6]) for r in rows[:-1]]
        if len(closes) >= 15:
            trs = [
                max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
                for i in range(len(closes) - 14, len(closes))
            ]
            if closes[-1] > 0:
                facts["atr_14d_pct"] = round(sum(trs) / len(trs) / closes[-1], 4)
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
                "&intervalTime=1h&limit=49"
            )
        )
        oi.sort(key=lambda r: int(r["timestamp"]))
        if oi:
            # The venue reports OI in contracts; notional is contracts x price.
            oi_latest = float(oi[-1]["openInterest"])
            turnover = facts.get("turnover_24h_usdt")
            price = facts.get("last_price")
            if (
                oi_latest > 0
                and isinstance(turnover, (int, float))
                and isinstance(price, (int, float))
                and price > 0
            ):
                facts["turnover_to_oi_24h"] = round(
                    float(turnover) / (oi_latest * float(price)), 1
                )
        if len(oi) >= 25:
            last = float(oi[-1]["openInterest"])
            first24 = float(oi[-25]["openInterest"])
            if first24 > 0:
                facts["oi_change_24h_pct"] = round((last / first24 - 1.0) * 100, 2)
            if len(oi) >= 49:
                first48 = float(oi[0]["openInterest"])
                if first48 > 0:
                    facts["oi_change_48h_pct"] = round((last / first48 - 1.0) * 100, 2)
    except Exception:
        pass
    try:
        # The premium's own path, not just its level: the rubric classifies
        # leverage vs organic flow, and a premium that is rising into a pump
        # reads differently from one that is collapsing even at the same
        # print. Hourly premium-index kline closes; [4] is the close.
        pk = _result_list(
            _http_json(
                f"{BYBIT_PUBLIC}/v5/market/premium-index-price-kline"
                f"?category=linear&symbol={symbol}&interval=60&limit=25"
            )
        )
        if len(pk) >= 25:
            pk.sort(key=lambda r: int(r[0]))
            px24 = float(pk[-25][4])
            if px24 != 0.0:
                facts["premium_bp_24h_ago"] = round(px24 * 10_000, 1)
                now_bp = facts.get("perp_premium_bp")
                if now_bp is not None:
                    facts["premium_change_24h_bp"] = round(float(now_bp) - px24 * 10_000, 1)
    except Exception:
        pass
    try:
        # Binance's taker buy volume over taker sell volume. Bybit publishes no
        # equivalent, and the ticker symbols agree often enough to be worth
        # asking -- a name Binance does not list simply stays null, which is
        # what every other optional fact does.
        #
        # The MEAN of the five-minute ratios over the last completed UTC day,
        # not the day aggregate. Those are different numbers -- a mean of
        # ratios sits above a ratio of sums -- and the mean is the one the
        # threshold in the rubric was measured on.
        rows = _http_json(
            f"{BINANCE_PUBLIC}/futures/data/takerlongshortRatio"
            f"?symbol={symbol}&period=5m&limit=500"
        )
        midnight_ms = int(
            dt.datetime.now(dt.timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
            * 1000
        )
        mean = taker_ratio_day_mean(rows, midnight_ms)
        if mean is not None:
            facts["taker_buy_sell_ratio_1d"] = mean
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
TRIGGER_SUPPRESSION_HOURS = 24

# The freshness veto (change point 2026-08-30). A trigger whose name this
# ledger already flagged on two or more distinct earlier UTC days inside the
# window is a move in at least its third day, and the gate does not publish
# it: the one measured gate loss class is exactly that chase (AAVE 2026-08-24,
# scored 7 on its third consecutive mover-day after a +45% three-day run,
# bought within 2.5% of the top). The judgment row is still journaled in
# full with the veto marked, so --grade reads the vetoed and published
# cohorts side by side — that ledger split IS the forward A/B.
GATE_FRESHNESS_WINDOW_DAYS = 4
GATE_FRESHNESS_VETO_PRIOR_DAYS = 2


def _flag_days_by_symbol(path: Path, now: dt.datetime, *, window_days: int) -> dict[str, set[str]]:
    """Distinct EARLIER UTC dates each symbol was flagged (mover or trigger).

    Today's own rows are excluded, so a pump's second judged hour cannot veto
    its first day; only prior calendar days count as staleness.
    """

    days: dict[str, set[str]] = {}
    if not path.exists():
        return days
    today = now.date()
    cutoff = today - dt.timedelta(days=window_days)
    for line in path.open():
        try:
            row = json.loads(line)
            if row.get("row_type", "mover") not in ("mover", "trigger"):
                continue
            day = dt.datetime.fromisoformat(row["ts_utc"]).date()
            if not cutoff <= day < today:
                continue
            symbol = str(row["facts"]["symbol"])
        except Exception:
            continue
        days.setdefault(symbol, set()).add(day.isoformat())
    return days


def _recently_nominated(path: Path, now: dt.datetime, *, hours: int, row_type: str | None = None) -> set[str]:
    """Symbols already journaled inside the suppression window: one pump, one
    ledger row — re-rows of the same move would only inflate n with
    correlated points."""

    if not path.exists():
        return set()
    cutoff = now - dt.timedelta(hours=hours)
    recent: set[str] = set()
    for line in path.open():
        try:
            row = json.loads(line)
            if row_type is not None and row.get("row_type", "mover") != row_type:
                continue
            if dt.datetime.fromisoformat(row["ts_utc"]) >= cutoff:
                recent.add(str(row["facts"]["symbol"]))
        except Exception:
            continue
    return recent


def cmd_once(ledger_dir: Path) -> None:
    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / "ledger.jsonl"
    now = dt.datetime.now(dt.timezone.utc)
    recent = _recently_nominated(path, now, hours=RENOMINATION_HOURS, row_type="mover")
    nominees = [f for f in nominate() if f["symbol"] not in recent]
    with path.open("a") as fh:
        for facts in nominees:
            judgment = judge(facts)
            row = {
                "ts_utc": now.isoformat(timespec="seconds"),
                "prompt_version": PROMPT_VERSION,
                "row_type": "mover",
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


WOULD_ENTER_SCORE = 6

# Detection horizons in hours. A window's bar is the daily 2.5-sigma trigger
# scaled by sqrt(h/24), the same variance scaling the registered 3d/7d
# triggers use in the other direction.
#
# Nothing below 4h: graded on 5.5 years of hourly bars, the 1h and 2h windows
# each have a significantly negative year and 12h has none.
TRIGGER_WINDOWS_H = (4, 12, 24)
# Bounds LLM spend per run, not admission. Raised 10 -> 20 with the wide
# band so a hot hour cannot starve ranks 11-30 out of the journal.
TRIGGER_ROWS_MAX = 20


def _completed_hourly(symbol: str, limit: int = 26) -> list[list[Any]]:
    rows = _result_list(
        _http_json(
            f"{BYBIT_PUBLIC}/v5/market/kline?category=linear&symbol={symbol}&interval=60&limit={limit}"
        )
    )
    rows.sort(key=lambda r: int(r[0]))
    now_ms = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    return [r for r in rows if int(r[0]) + 3_600_000 <= now_ms]


def _daily_regime_on(symbol: str) -> bool | None:
    rows = _result_list(
        _http_json(f"{BYBIT_PUBLIC}/v5/market/kline?category=linear&symbol={symbol}&interval=D&limit=32")
    )
    rows.sort(key=lambda r: int(r[0]))
    closes = [float(r[4]) for r in rows[:-1]]
    if len(closes) < 31:
        return None
    sma = sum(closes[-30:]) / 30.0
    return closes[-1] > sma


def cmd_triggers(ledger_dir: Path) -> None:
    """The shadow entry gate: the exact flow a live gate would run, pointed at
    the ledger. Universe and regime gates are public-data approximations of
    the registered daily rule; the promotion math re-derives on the journaled
    candidates, so an approximate nominator only costs coverage, never truth.
    """

    ledger_dir.mkdir(parents=True, exist_ok=True)
    path = ledger_dir / "ledger.jsonl"
    now = dt.datetime.now(dt.timezone.utc)
    btc_on = _daily_regime_on("BTCUSDT")
    eth_on = _daily_regime_on("ETHUSDT")
    if not (btc_on and eth_on):
        # Say so in the file too. Returning here without publishing would leave
        # the previous run's candidates standing for the rest of their 90
        # minutes, so the LONG sleeve would keep entering under a regime this
        # run has just declared off. A failed regime read (None) lands here as
        # well, and fails closed the same way.
        publish_gate_candidates([])
        print(f"regime off (btc={btc_on} eth={eth_on}); no triggers scanned, candidates cleared")
        return
    recent = _recently_nominated(path, now, hours=TRIGGER_SUPPRESSION_HOURS, row_type="trigger")
    flag_days = _flag_days_by_symbol(path, now, window_days=GATE_FRESHNESS_WINDOW_DAYS)

    body = _http_json(f"{BYBIT_PUBLIC}/v5/market/tickers?category=linear")
    rows = _result_list(body)
    usdt = [r for r in rows if str(r.get("symbol", "")).endswith("USDT")]
    by_symbol = {str(r["symbol"]): r for r in usdt}
    usdt.sort(key=lambda r: -float(r.get("turnover24h") or 0.0))

    def _move_of(symbol: str) -> float | None:
        row = by_symbol.get(symbol)
        return round(float(row.get("price24hPcnt") or 0.0), 4) if row else None

    fired = 0
    judged_events: list[dict[str, Any]] = []
    with path.open("a") as fh:
        for rank, t in enumerate(usdt[:TRIGGER_WIDE_RANK_MAX], start=1):
            symbol = str(t["symbol"])
            if symbol in recent:
                continue
            try:
                hourly = _completed_hourly(symbol)
            except Exception:
                continue
            if len(hourly) < 25:
                continue
            closes = [float(r[4]) for r in hourly]
            all_highs = [float(r[2]) for r in hourly]
            all_lows = [float(r[3]) for r in hourly]
            trigger_close = closes[-1]
            if trigger_close <= 0:
                continue
            window_stats: dict[int, tuple[float, float]] = {}
            for h in TRIGGER_WINDOWS_H:
                base = closes[-1 - h]
                if base <= 0:
                    continue
                ret_h = math.log(trigger_close / base)
                hi = max(all_highs[-h:])
                lo = min(all_lows[-h:])
                loc_h = (trigger_close - lo) / (hi - lo) if hi > lo else 0.5
                window_stats[h] = (ret_h, loc_h)
            if not any(
                ret > 0.0 and loc >= 0.70 for ret, loc in window_stats.values()
            ):
                continue
            roll_ret = window_stats.get(24, (0.0, 0.5))[0]
            facts: dict[str, Any] = {
                "symbol": symbol,
                "move_24h": round(math.expm1(roll_ret), 4),
                "turnover_24h_usdt": round(float(t.get("turnover24h") or 0.0), 0),
                "turnover_rank": rank,
                "last_price": trigger_close,
                "trigger_price": trigger_close,
                "trigger_bar_end_utc": dt.datetime.fromtimestamp(
                    (int(hourly[-1][0]) + 3_600_000) / 1000, tz=dt.timezone.utc
                ).isoformat(timespec="seconds"),
                "funding_rate": float(t.get("fundingRate") or 0.0),
                "btc_move_24h": _move_of("BTCUSDT"),
                "eth_move_24h": _move_of("ETHUSDT"),
                "hour_utc": now.hour,
            }
            mark = float(t.get("markPrice") or 0.0)
            index = float(t.get("indexPrice") or 0.0)
            if index > 0:
                facts["perp_premium_bp"] = round((mark / index - 1.0) * 10_000, 1)
            if facts["btc_move_24h"] is not None:
                facts["idio_move_24h"] = round(facts["move_24h"] - facts["btc_move_24h"], 4)
            facts = enrich(symbol, facts)
            sigma = facts.get("sigma_daily_30d")
            # enrich derives depth from move_24h == the rolling return here, so
            # the depth gate and the judged fact agree by construction.
            depth = facts.get("depth_ratio")
            atr_pct = facts.get("atr_14d_pct")
            if not isinstance(sigma, float) or sigma <= 0.0:
                continue
            if not isinstance(atr_pct, float) or atr_pct <= 0.0 or atr_pct > 0.12:
                continue
            windows_fired = []
            for h in TRIGGER_WINDOWS_H:
                stats = window_stats.get(h)
                if stats is None:
                    continue
                ret_h, loc_h = stats
                bar_h = 2.5 * sigma * math.sqrt(h / 24.0)
                if bar_h > 0 and loc_h >= 0.70 and ret_h / bar_h >= 1.0:
                    windows_fired.append(h)
                    facts[f"move_{h}h"] = round(math.expm1(ret_h), 4)
                    facts[f"depth_{h}h"] = round(ret_h / bar_h, 2)
            if not windows_fired:
                continue
            fastest = min(windows_fired)
            facts["windows_fired_h"] = windows_fired
            facts["trigger_window_h"] = fastest
            facts["range_location"] = round(window_stats[fastest][1], 3)
            depth = facts.get(f"depth_{fastest}h")
            judgment = judge(facts)
            score = (judgment or {}).get("pump_quality_score")
            would_enter = isinstance(score, (int, float)) and score >= WOULD_ENTER_SCORE
            prior_days = len(flag_days.get(symbol, ()))
            freshness_veto = prior_days >= GATE_FRESHNESS_VETO_PRIOR_DAYS
            rank_band = "core" if rank <= TRIGGER_TURNOVER_RANK_MAX else "wide"
            row = {
                "ts_utc": now.isoformat(timespec="seconds"),
                "prompt_version": PROMPT_VERSION,
                "row_type": "trigger",
                "would_enter": bool(would_enter),
                "would_enter_score_min": WOULD_ENTER_SCORE,
                "freshness_veto": freshness_veto,
                "prior_flag_days": prior_days,
                "rank_band": rank_band,
                "facts": facts,
                "judgment": judgment,
            }
            fh.write(json.dumps(row) + "\n")
            judged_events.append(row)
            fired += 1
            verdict = (
                f"score={score} would_enter={'YES' if would_enter else 'no'}"
                if judgment
                else "unjudged"
            )
            if freshness_veto:
                verdict += f" VETOED (flagged on {prior_days} earlier days)"
            print(
                f"TRIGGER {symbol:<14} {facts['trigger_window_h']}h window "
                f"+{facts[f'move_{fastest}h']:.0%} depth={depth}  {verdict}"
            )
            if fired >= TRIGGER_ROWS_MAX:
                print("trigger row cap reached this run")
                break
    print(f"{fired} trigger event(s) journaled")

    published = publish_gate_candidates(judged_events)
    for event in published:
        print(f"GATE candidate {event['symbol']:<14} score={event['score']}")


# ---------------------------------------------------------------------------
# The candidates file (owner-directed integration, demo fleet only).
#
# Every score >= 6 trigger event is published to the research candidates file.
# The native LONG runtime does not read it. This script stays credential-free
# and order-free. Every run that reaches a verdict publishes,
# including the empty verdict: a fresh file saying "no candidates" is what
# stops the previous run's names being entered for the rest of their validity.
# Research consumers treat a missing or stale file as "no signal".
# ---------------------------------------------------------------------------

GATE_CANDIDATES_PATH = (
    "/var/lib/liquidity-migration/llm-driver-ledger/llm-gate-candidates.json"
)
GATE_CANDIDATES_VALID_MIN = 60


def _write_json_atomic(path: Path, payload: Any) -> None:
    durable_atomic_replace(
        path,
        (json.dumps(payload, indent=1, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o640,
        label="LLM gate candidates",
    )


def publish_gate_candidates(
    judged_events: list[dict[str, Any]],
    *,
    path: str = GATE_CANDIDATES_PATH,
) -> list[dict[str, Any]]:
    """Write the would_enter events the LONG sleeve may enter, and return them."""

    now = dt.datetime.now(dt.timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    events: list[dict[str, Any]] = []
    for ev in judged_events:
        if not ev.get("would_enter"):
            continue
        if ev.get("freshness_veto"):
            # Journaled in full, published never: the vetoed cohort's forward
            # returns grade the veto itself.
            continue
        facts = ev.get("facts") or {}
        atr = facts.get("atr_14d_pct")
        price = facts.get("trigger_price")
        if not isinstance(atr, (int, float)) or atr <= 0.0:
            continue
        if not isinstance(price, (int, float)) or price <= 0.0:
            continue
        try:
            trigger_ts_ms = int(
                dt.datetime.fromisoformat(str(facts["trigger_bar_end_utc"]))
                .timestamp()
                * 1000
            )
        except Exception:
            trigger_ts_ms = now_ms
        events.append(
            {
                "symbol": str(facts.get("symbol", "")).upper(),
                "score": (ev.get("judgment") or {}).get("pump_quality_score"),
                "band": str(ev.get("rank_band") or "core"),
                "trigger_ts_ms": trigger_ts_ms,
                "trigger_price": float(price),
                "atr_pct": float(atr),
                "sigma_daily_30d": facts.get("sigma_daily_30d"),
                "turnover_rank": facts.get("turnover_rank"),
                "trigger_window_h": facts.get("trigger_window_h"),
            }
        )
    payload = {
        "decision_ts_ms": now_ms,
        "valid_until_ms": now_ms + GATE_CANDIDATES_VALID_MIN * 60_000,
        "events": events,
    }
    _write_json_atomic(Path(path), payload)
    return events


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
        if row.get("row_type") == "gate_action":
            continue
        judgment = row.get("judgment") or {}
        row_type = str(row.get("row_type", "mover"))
        if row.get("freshness_veto"):
            row_type += "[vetoed]"
        if row.get("rank_band") == "wide":
            row_type += "[wide]"
        kind = f"{row_type}:{judgment.get('driver_kind', 'unjudged')}"
        version = str(row.get("prompt_version", "?"))
        fwd = _forward_return(row["facts"]["symbol"], int(ts.timestamp() * 1000), 72)
        if fwd is None:
            continue
        buckets.setdefault((version, kind), []).append(fwd)
        score = judgment.get("pump_quality_score")
        if isinstance(score, (int, float)):
            band = "score 0-3" if score <= 3 else ("score 4-6" if score <= 6 else "score 7-10")
            buckets.setdefault((version, f"{row_type}:{band}"), []).append(fwd)
        graded += 1
    print(f"graded {graded} row(s) at the 72h horizon:")
    for (version, kind), vals in sorted(buckets.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
        mean = sum(vals) / len(vals)
        wins = sum(1 for v in vals if v > 0) / len(vals)
        print(f"  {version:<26} {kind:<14} n={len(vals):<4} mean {mean:+.2%}  win {wins:.0%}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="nominate, enrich, and judge current movers")
    parser.add_argument("--triggers", action="store_true", help="judge fresh intraday trigger events; publish score >= 6 to the LONG sleeve's candidates file")
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
    if args.triggers:
        cmd_triggers(ledger_dir)
    if args.grade:
        cmd_grade(ledger_dir)
    if not (args.once or args.triggers or args.grade):
        parser.error("pass --once, --triggers, or --grade")


if __name__ == "__main__":
    main()
