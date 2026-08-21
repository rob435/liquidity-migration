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
  --triggers  the shadow entry gate, run hourly: detect fresh intraday
           deep-trigger events (rolling 24h window, the 2.5-sigma family,
           regime and ATR gates approximated from public data), judge each,
           and journal the would-be entry with its trigger-hour price and a
           provisional would_enter at score >= 7. The same flow a live gate
           would run, pointed at the ledger instead of the order book.
  --grade  for ledger rows at least 3 days old, fetch what actually happened
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

from liquidity_migration.rules.engine_targets import TARGET_BOOK_VERSION

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
TRIGGER_SUPPRESSION_HOURS = 24


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

# Detection horizons in hours. The daily program measured only the 24h
# window; the shorter horizons are untested and the ledger is where they
# earn or lose a record. A window's bar is the daily 2.5-sigma trigger
# scaled by sqrt(h/24), the same variance scaling the registered 3d/7d
# triggers use in the other direction.
TRIGGER_WINDOWS_H = (1, 2, 4, 12, 24)
TRIGGER_ROWS_MAX = 10


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
        print(f"regime off (btc={btc_on} eth={eth_on}); no triggers scanned")
        return
    recent = _recently_nominated(path, now, hours=TRIGGER_SUPPRESSION_HOURS, row_type="trigger")

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
        for rank, t in enumerate(usdt[:MAX_TURNOVER_RANK], start=1):
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
            row = {
                "ts_utc": now.isoformat(timespec="seconds"),
                "prompt_version": PROMPT_VERSION,
                "row_type": "trigger",
                "would_enter": bool(would_enter),
                "would_enter_score_min": WOULD_ENTER_SCORE,
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
            print(
                f"TRIGGER {symbol:<14} {facts['trigger_window_h']}h window "
                f"+{facts[f'move_{fastest}h']:.0%} depth={depth}  {verdict}"
            )
            if fired >= TRIGGER_ROWS_MAX:
                print("trigger row cap reached this run")
                break
    print(f"{fired} trigger event(s) journaled")

    prices: dict[str, float] = {}
    for r in usdt:
        try:
            prices[str(r["symbol"])] = float(r.get("lastPrice") or 0.0)
        except Exception:
            continue
    actions = gate_run(ledger_dir, judged_events, prices, now)
    if actions:
        with path.open("a") as fh:
            for action in actions:
                fh.write(
                    json.dumps(
                        {
                            "ts_utc": now.isoformat(timespec="seconds"),
                            "row_type": "gate_action",
                            **action,
                        }
                    )
                    + "\n"
                )
                print(f"GATE {action['action']} {action['symbol']}")


# ---------------------------------------------------------------------------
# The live entry gate (owner-directed 2026-08-21, demo fleet only).
#
# When LLM_GATE_LIVE is on, a would_enter trigger event becomes a real entry
# target in this sleeve's own book (`long_llm_gate_v1`, engine strategy
# "llm_gate"), sized from the engine heartbeat's equity. The ledger holds no
# venue credentials, so exits are managed by PRICE against public tickers on
# the hourly grid -- the venue-native wide stop declared on every entry row is
# the always-armed backstop underneath. Every failure (no key, no heartbeat,
# stale heartbeat, API down) fails closed to "no entry"; exits keep flowing.
#
# Known, accepted coarseness (demo money, owner call): a wide-stop fill is
# inferred from price, so between the venue stop firing and the next hourly
# check a standing target can re-open the position once at the same size and
# stop. LLM_GATE_LIVE off stops entries but keeps managing exits;
# LLM_GATE_DRAIN=1 zeroes the whole book now.
# ---------------------------------------------------------------------------

GATE_SOURCE = "long_llm_gate_v1"
GATE_BOOK_PATH = "/var/lib/liquidity-migration/targets/llm-gate-demo.json"
GATE_SIBLING_BOOKS = (
    "/var/lib/liquidity-migration/targets/carry-demo.json",
    "/var/lib/liquidity-migration/targets/long-demo.json",
    "/var/lib/liquidity-migration/targets/exodus-demo.json",
)
GATE_HEARTBEAT_PATH = "/var/lib/liquidity-migration-engine/heartbeat.json"
GATE_MAX_CONCURRENT = 5
GATE_COOLDOWN_H = 168
GATE_SLOT_FRACTION = 0.05
GATE_LEVERAGE = 2.0
GATE_STOP_ATR_MULT = 3.0
GATE_DECAY_H = 48
GATE_DECAY_ATR_MULT = 1.5
GATE_TP_ATR_MULT = 4.0
GATE_MAX_HOLD_H = 72
GATE_MIN_NOTIONAL_USDT = 15.0
GATE_BOOK_VALID_MIN = 75
GATE_CLOSING_GRACE_H = 6
GATE_HEARTBEAT_MAX_AGE_S = 180


def _env_on(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "on", "true", "yes")


def _load_json(path: str) -> Any:
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def _write_json_atomic(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
    tmp.replace(path)


def gate_read_equity(now_ms: int, heartbeat_path: str = GATE_HEARTBEAT_PATH) -> float | None:
    """The engine heartbeat's equity, or None when missing or stale (no entry
    without a live read -- the same gate the producers apply)."""

    hb = _load_json(heartbeat_path)
    if not isinstance(hb, dict):
        return None
    equity = hb.get("account_equity_usdt")
    wall = hb.get("wall_ts_ms")
    if not isinstance(equity, (int, float)) or equity <= 0.0:
        return None
    if not isinstance(wall, (int, float)) or now_ms - float(wall) > GATE_HEARTBEAT_MAX_AGE_S * 1000:
        return None
    return float(equity)


def gate_sibling_symbols(paths: tuple[str, ...] = GATE_SIBLING_BOOKS) -> set[str]:
    held: set[str] = set()
    for p in paths:
        book = _load_json(p)
        if not isinstance(book, dict):
            continue
        for row in book.get("targets") or []:
            try:
                if abs(float(row.get("notional_usdt", 0.0))) > 0.0:
                    held.add(str(row["symbol"]))
            except Exception:
                continue
    return held


def gate_entry_notional(equity: float, sigma_daily: float | None) -> float:
    """v12's slot shape at half scale: 5% of equity times the vol-parity
    weight clamped to [0.25, 1.0]."""

    weight = 1.0
    if isinstance(sigma_daily, (int, float)) and sigma_daily > 0.0:
        vol_ann = float(sigma_daily) * math.sqrt(365.0)
        weight = max(0.25, min(0.30 / max(vol_ann, 1e-9), 1.0))
    return equity * GATE_SLOT_FRACTION * weight


def gate_exit_reason(component: dict[str, Any], price: float | None, now_ms: int) -> str | None:
    """First exit that applies, in the order the money cares about."""

    entry_ts = int(component["entry_ts_ms"])
    ref = float(component["trigger_price"])
    atr = float(component["atr_pct"])
    age_h = (now_ms - entry_ts) / 3_600_000
    if price is not None and price > 0.0:
        if price <= ref * (1.0 - GATE_STOP_ATR_MULT * atr):
            return "wide_stop_assumed"
        if age_h >= GATE_DECAY_H and price <= ref * (1.0 - GATE_DECAY_ATR_MULT * atr):
            return "decayed_stop"
        if price >= ref * (1.0 + GATE_TP_ATR_MULT * atr):
            return "take_profit"
    if age_h >= GATE_MAX_HOLD_H:
        return "time_stop"
    return None


def gate_render_book(state: dict[str, Any], now_ms: int) -> dict[str, Any]:
    rows = []
    for symbol in sorted(state.get("held", {})):
        c = state["held"][symbol]
        rows.append(
            {
                "symbol": symbol,
                "notional_usdt": round(float(c["notional_usdt"]), 2),
                "leverage": GATE_LEVERAGE,
                "stop_loss_fraction": round(GATE_STOP_ATR_MULT * float(c["atr_pct"]), 6),
            }
        )
    for symbol in sorted(state.get("closing", {})):
        c = state["closing"][symbol]
        rows.append(
            {
                "symbol": symbol,
                "notional_usdt": 0.0,
                "leverage": GATE_LEVERAGE,
                "stop_loss_fraction": round(GATE_STOP_ATR_MULT * float(c["atr_pct"]), 6),
            }
        )
    return {
        "decision_ts_ms": now_ms,
        "source": GATE_SOURCE,
        "targets": rows,
        "valid_until_ms": now_ms + GATE_BOOK_VALID_MIN * 60_000,
        # The engine refuses a book without a whole-number version.
        "version": TARGET_BOOK_VERSION,
    }


def gate_run(
    ledger_dir: Path,
    judged_events: list[dict[str, Any]],
    prices: dict[str, float],
    now: dt.datetime,
    *,
    book_path: str = GATE_BOOK_PATH,
    heartbeat_path: str = GATE_HEARTBEAT_PATH,
    sibling_paths: tuple[str, ...] = GATE_SIBLING_BOOKS,
) -> list[dict[str, Any]]:
    """One gate cycle: exits by price, entries from judged events, book out.
    Returns action rows for the ledger."""

    now_ms = int(now.timestamp() * 1000)
    live = _env_on("LLM_GATE_LIVE")
    drain = _env_on("LLM_GATE_DRAIN")
    state_path = ledger_dir / "gate_state.json"
    state = _load_json(str(state_path)) or {}
    state.setdefault("held", {})
    state.setdefault("closing", {})
    state.setdefault("cooldown_until_ms", {})
    if not live and not drain and not state["held"] and not state["closing"]:
        return []

    actions: list[dict[str, Any]] = []

    def _close(symbol: str, reason: str) -> None:
        c = state["held"].pop(symbol)
        c["closed_ts_ms"] = now_ms
        c["exit_reason"] = reason
        state["closing"][symbol] = c
        state["cooldown_until_ms"][symbol] = now_ms + GATE_COOLDOWN_H * 3_600_000
        actions.append({"action": f"exit:{reason}", "symbol": symbol, "age_h": round((now_ms - int(c["entry_ts_ms"])) / 3_600_000, 1)})

    if drain:
        for symbol in list(state["held"]):
            _close(symbol, "drain")
    else:
        for symbol in list(state["held"]):
            reason = gate_exit_reason(state["held"][symbol], prices.get(symbol), now_ms)
            if reason:
                _close(symbol, reason)

    for symbol in list(state["closing"]):
        if now_ms - int(state["closing"][symbol].get("closed_ts_ms", now_ms)) > GATE_CLOSING_GRACE_H * 3_600_000:
            del state["closing"][symbol]

    if live and not drain:
        equity = gate_read_equity(now_ms, heartbeat_path)
        siblings = gate_sibling_symbols(sibling_paths)
        for ev in judged_events:
            if not ev.get("would_enter"):
                continue
            facts = ev["facts"]
            symbol = str(facts["symbol"])
            if len(state["held"]) >= GATE_MAX_CONCURRENT:
                actions.append({"action": "skip:capacity", "symbol": symbol})
                continue
            if symbol in state["held"] or symbol in state["closing"]:
                continue
            if int(state["cooldown_until_ms"].get(symbol, 0)) > now_ms:
                actions.append({"action": "skip:cooldown", "symbol": symbol})
                continue
            if symbol in siblings:
                actions.append({"action": "skip:sibling_holds", "symbol": symbol})
                continue
            if equity is None:
                actions.append({"action": "skip:no_equity_read", "symbol": symbol})
                continue
            atr = facts.get("atr_14d_pct")
            price = facts.get("trigger_price")
            if not isinstance(atr, (int, float)) or atr <= 0.0 or not isinstance(price, (int, float)) or price <= 0.0:
                continue
            notional = gate_entry_notional(equity, facts.get("sigma_daily_30d"))
            if notional < GATE_MIN_NOTIONAL_USDT:
                actions.append({"action": "skip:below_min_notional", "symbol": symbol})
                continue
            state["held"][symbol] = {
                "entry_ts_ms": now_ms,
                "trigger_price": float(price),
                "atr_pct": float(atr),
                "notional_usdt": round(notional, 2),
                "score": (ev.get("judgment") or {}).get("pump_quality_score"),
                "trigger_window_h": facts.get("trigger_window_h"),
            }
            actions.append(
                {
                    "action": "entry",
                    "symbol": symbol,
                    "notional_usdt": round(notional, 2),
                    "stop_loss_fraction": round(GATE_STOP_ATR_MULT * float(atr), 6),
                    "score": (ev.get("judgment") or {}).get("pump_quality_score"),
                }
            )

    book = gate_render_book(state, now_ms)
    _write_json_atomic(Path(book_path), book)
    _write_json_atomic(state_path, state)
    return actions


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
    parser.add_argument("--triggers", action="store_true", help="shadow entry gate: judge fresh intraday trigger events")
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
