# Research Log — Liquidity-Migration Salvageability Study

Executing `docs/research_plan.md`. Every backtest is pre-registered here BEFORE
it runs: hypothesis, pass/fail threshold, what I expect, and what the opposite
result would mean. No post-hoc threshold moves. Methodology law: `docs/research_plan.md` §2.

Session start: 2026-05-20. Author: research session (Opus 4.7).

---

## WS-0 — Reproduce and harden the harness

### WS-0a — Data roots

`~/SHARED_DATA` was empty on this machine — all three PIT roots rebuilt from
scratch (the plan assumed `bybit_fullpit_1h` already existed; it did not).

- `binance_oos_pit` — built: 3,097,956 kline rows / 198 symbols. Matches the
  v2 report's "3.1M kline / 198 symbols" exactly → data pipeline reproducible.
- `bybit_fullpit_1h` — manifest built (372,049 rows / 465 symbols); kline
  download in progress.
- `bybit_oos_pre2023` — manifest built (91,540 rows / 216 symbols); kline
  download pending.

**Ops note — a real bug found and fixed (storage.py).** The IS kline download
repeatedly stalled. Chased it down properly rather than restarting blindly:

1. First suspected Bybit API rate-limit contention (24 concurrent workers).
   Reduced to 12, serialised — still stalled. Single manual API requests were
   fast (0.3s), so not an IP ban.
2. Instrumented the per-symbol download by phase: scan 0.00s, fetch 0.50s —
   both fine. The hang was in the per-date `write_dataset` calls.
3. Root cause: a stale lock file `bybit_fullpit_1h/.locks/klines_1h.lock`
   naming pid 1028 (a download process killed mid-write). `write_dataset` /
   `read_dataset` take this lock. Stale-lock recovery calls
   `_lock_owner_is_dead`, which uses `os.kill(pid, 0)` — but on Windows a
   non-existent pid raises `OSError winerror 87`, not `ProcessLookupError`.
   The `except OSError: return False` branch treated the dead owner as ALIVE,
   so every read/write blocked until the 6h stale timeout.

Fix: `_lock_owner_is_dead` now treats `os.kill` winerror 87 as a dead owner
(`liquidity_migration/storage.py`), with regression test
`test_exclusive_file_lock_recovers_windows_winerror_87_dead_pid`. The pre-
existing dead-pid test used pid 2_147_483_647, which trips `os.kill`'s
`OverflowError` path on Windows and so never exercised normal-range pids —
which is how the bug survived. Stale locks cleared; IS download then flowed.

No research impact on data identity — this only affected build wall-clock —
but it is a genuine cross-platform correctness bug in stale-lock recovery and
worth the fix on its own merits.

### WS-0b — IC diagnostic tool — DONE

Built `liquidity_migration/ic_diagnostic.py` + `tests/test_ic_diagnostic.py`
(11 tests, all pass).

The reported bug — composite IC `+0.176` on Binance, ~4× its best component
(~0.04) — is mathematically impossible for an equal-weight mean (averaging
ceiling is √k ≈ 2×). Most likely cause: the composite was scored by *pooling*
all (symbol, day) observations into one correlation while the components were
scored per-day; pooling lets a between-day regime confound masquerade as
cross-sectional skill.

Fix is structural: `cross_sectional_ic` computes a per-day cross-sectional
Spearman (≥10 names/day) and averages across days; the composite is just
another column through the *identical* path — no separate code path exists to
reintroduce the bug. Regression guard `test_ic_is_per_day_not_pooled`
constructs a panel with per-day IC = +1.0 every day but a strongly negative
pooled correlation; the correct tool must (and does) report +1.0.

### WS-0c — Reproduce harness numbers — PRE-REGISTRATION

**Hypothesis.** `reversion_alpha.run_reversion_backtest` with the default
`ReversionConfig` (4-signal composite, top decile, continuous regime scaler,
28.8 bps round-trip) reproduces the four-window table in
`docs/reversion_alpha_report.md`:

| Window | Trades | Return | Max DD | Sharpe | Win |
|---|--:|--:|--:|--:|--:|
| IS-train Bybit 2023-09→2024-09 | 661 | −28.5% | −41.5% | −0.76 | 50.4% |
| IS-valid Bybit 2024-09→2026-05 | 1154 | +115.4% | −25.4% | +1.37 | 51.1% |
| OOS Bybit 2022-04→2023-05 | 681 | +33.7% | −41.2% | +0.76 | 52.6% |
| OOS Binance 2020-09→2023-05 | 1606 | −77.5% | −87.0% | −0.54 | 44.7% |

**Pass threshold (pre-registered).** Reproduction passes for a window if total
return is within ±5 pp and trade count within ±5% of the report. Same code +
same data-build process should give near-exact numbers; the tolerance only
absorbs minor archive-availability drift across a fresh rebuild.

**What I expect.** Rough reproduction on all four. The Binance root already
rebuilt to a row count matching the v2 report exactly, so the data is the same
input — I expect Binance OOS ≈ −77% give or take a couple pp.

**What the opposite would mean.** A wildly different number (e.g. Binance OOS
positive, or trade count off by 2×) means the rebuilt root differs from the
report's root, or the harness is non-deterministic, or the report is wrong.
Any of those halts WS-0 — "nothing downstream is valid" (plan §4 WS-0 gate).

#### Results

**oos_binance — PASS (near-exact reproduction).** Runtime 219s; scored_rows
109,310; book_rows 9,430.

| metric | reproduced | reported |
|---|--:|--:|
| trades | 1606 | 1606 |
| total_return | −77.47% | −77.50% |
| max_drawdown | −87.01% | −87.00% |
| sharpe | −0.544 | −0.54 |
| win_rate | 44.71% | 44.70% |

Trade count matches exactly and every metric is within rounding. The harness is
deterministic and the rebuilt Binance root is faithful to the report's root.
The −77.5% Binance OOS loss is a real, reproducible result — not a data artifact.

_is_train / is_valid / oos_bybit pending their data roots._

**oos_binance — legacy vs fixed entry (entry-lag fix re-baseline).**

| metric | legacy (delay=24h) | fixed (delay=1h) |
|---|--:|--:|
| trades | 1606 | 1632 |
| total_return | −77.47% | **−45.83%** |
| max_drawdown | −87.0% | −79.3% |
| sharpe | −0.54 | **−0.01** |
| win_rate | 44.7% | 46.0% |

Legacy reproduces the report exactly → the entry-timing refactor is clean
(`entry_delay_hours=24` ≡ the pre-fix code). The fix lifts Binance OOS by
**+31.6 pp** and takes Sharpe from clearly-negative to ~flat.

**Honest read.** The entry-lag fix is a real, large lever — worth ~32 pp on
this window, comparable to a big cost change, and free (a bug fix). It
vindicates the thesis that the reversion edge is freshest right after the
signal. BUT it does not make the strategy viable: −45.8% is still a large loss
and Sharpe ~0 is break-even-at-best. The fix is necessary for any honest
measurement; it is not sufficient. The 2021 alt-bull squeeze inside this window
is a *regime* problem the entry-lag fix cannot touch — that is WS-3's job.

Implication for the plan's framing: the plan's "cost ≈ edge, ~break-even" (§3)
was measured on the buggy 25h-lag harness. The corrected edge is bigger than
the plan assumed. The plan's pessimism was partly a lag artifact — though the
strategy is still not yet viable.

### WS-0a addendum — the parallel downloader is broken on Windows

A third infra bug, and the real cause of the whole build saga:
`storage.exclusive_file_lock` cannot survive concurrency on Windows. The lock
holder's `lock_path.unlink()` raises `PermissionError WinError 32` ("file in
use by another process") whenever a concurrent waiter holds a transient handle
on the lock file (every waiter opens it to read the owner pid). It works
single-process (the Binance build, all single-process reproductions) and fails
under any parallel workers — threads OR processes.

NOT fixing `exclusive_file_lock`: it is load-bearing for data integrity across
the whole repo (every read/write, the live demo + risk daemons). A subtle bug
in it would corrupt every backtest tonight. Instead: build each root with one
single-threaded process. Different roots have different lock files, so the IS
and Bybit-OOS roots build concurrently without contention. Slower wall-clock,
zero risk to data integrity. (Logged as a known repo bug for a later dedicated
fix — a Windows-safe lock needs `unlink` retry + a non-handle-holding wait.)

### WS-0d — entry-lag bug found (NOT in the plan — see deviation note)

Validating the IC tool on the Binance scored panel (`research/ws0_ic_validate.py`)
reproduced the report's signal *ordering* but every per-component IC came out
systematically lower (rank_jump 0.028 vs report 0.041; close_location even
flipped to −0.010 vs +0.003). A consistent one-directional gap is a buried
assumption, not noise — so I traced it (`research/ws0_entry_lag_probe.py`).

**Finding.** `reversion_alpha.simulate` enters trades ~25 hours after the
signal close, not the ~1 hour its own design intends.

Trace (BTCUSDT, Binance root): panel row `date=2021-01-06` summarises trading
day **2021-01-05** (last hourly bar 23:00, close realised at 2021-01-06 00:00).
`simulate` computes `entry_ts = to_datetime(date) + 23h + 1h = 2021-01-07
00:00` → **25h after the signal close**.

Root cause — a convention mismatch between two modules:
- `volume_features` / `volume_events` stamp each daily bar at `day_start + 24h`,
  so a panel row's `date` is the trading day **+1** (it labels the moment the
  day's data is complete).
- `reversion_alpha.simulate` does `entry_ts = day_ms + 23h + delay`, whose
  `+23h` only makes sense if `day_ms` were the trading day's 00:00. It is not —
  it is already the signal-close moment — so the `+23h` adds a spurious day.

This is a genuine bug, but **conservative** (entry is too late, never early →
not look-ahead), so the report's published backtests are not *invalid* — they
are the results of a strategy that trades a full day staler than intended. It
does mean the report has an internal inconsistency: its IC table and its P&L
were effectively measured at different entry lags.

**Why this matters / deviation from the plan.** The plan named cost (WS-1) and
holding horizon (WS-2) as the highest-leverage levers. Entry lag is a third,
the plan's author did not know about it, and it is *costless* to change — it is
a bug fix, not a parameter. For a reversion edge that decays over 1–5 days,
recovering the first ~24h is plausibly comparable to or larger than either
named lever. **Deviation:** WS-0 will fix this entry-lag bug as part of
"harden the harness", and WS-2 becomes a 2-D study — IC vs (entry lag × hold
horizon) — not horizon alone.

**Discipline note (am I steering toward a hopeful answer?).** The fix could cut
either way: fresher entry may capture more reversion, OR the first 24h after a
volume pump may be continuation/momentum and entering fresher means shorting a
still-running pump. I do not know which. The fix is made for *correctness* —
the harness should do what it documents — not because it is expected to help.
If the corrected harness still shows no edge, that is the answer.

**Fix design.** `day_ms = to_datetime(date)` already equals the signal-close
moment, so the correct entry is `entry_ts = day_ms + entry_delay_hours·h`
(drop the `+23h`). This keeps the report reproducible: the buggy run is exactly
`entry_delay_hours = 24` under the corrected code. New default `entry_delay_hours
= 1` = the intended "1h after signal close".

**Fix applied & validated.** `reversion_alpha.simulate` and
`ic_diagnostic.add_forward_short_returns` both corrected; tests updated
(`test_simulate_entry_timing_is_delay_after_signal_close` pins it); 348 → 350
tests still green.

Re-running the IC validation with the corrected 1h lag reproduces the report's
per-component ICs **near-exactly** — confirming both the fix and the IC tool:

| signal | IC @ 25h lag (buggy) | IC @ 1h lag (fixed) | report |
|---|--:|--:|--:|
| z_rank_jump | 0.0282 | **0.0409** | 0.041 |
| z_residual_return | 0.0081 | 0.0248 | 0.026 |
| z_turnover_ratio | 0.0064 | 0.0153 | 0.019 |
| z_close_location | −0.0101 | −0.0013 | 0.003 |
| reversion_score (composite) | 0.0060 | **0.0204** | (bug: +0.176) |

Confirmed: the report's IC table was measured at ~1h lag while its backtests
ran at 25h lag. The IC tool is correct. Two research takeaways already visible:
1. The honest best-single-signal IC is `z_rank_jump` ≈ **0.041** (t≈9.6,
   hit-rate 64% — weak but real and highly significant). `close_location` is
   dead (IC ≈ 0), a 4th independent confirmation.
2. The equal-weight composite (0.020) is *worse* than rank_jump alone (0.041) —
   equal-weighting dilutes the one good signal with three weak/dead ones. This
   is a flag for WS-4: the 4-signal equal-weight composite is not the right
   aggregation.

---

## WS-1 — Execution-cost study — PRE-REGISTRATION

**Hypothesis.** Round-trip cost is a binding constraint; if realistically-
achievable cost is materially below break-even, net edge becomes positive.

**Method.** On the corrected harness (entry_delay=1h), cost grid
{10,15,20,28.8,48,67} bps x stop_fill_mode {stop, bar_extreme}, all 4 windows.
Locate break-even cost per window.

**What the flat grid can and cannot do.** It SCREENS: if a window is negative
even at 10 bps, cost is not the lever (kill). It CANNOT confirm viability — a
low break-even cost does not prove that cost is achievable, because achievable
cost is set by the maker-fill / adverse-selection mix, which a flat per-trade
number cannot represent. A passive short entry on a still-pumping name is
adversely selected (you fill when the pump continues = when you are wrong).
Confirming achievable cost is intrinsically a WS-6 forward-test question.

**Pre-registered pass.** Break-even cost (stop-fill mode) >= 40 bps on BOTH
OOS windows, AND still net-positive under the bar_extreme adverse-fill stress.
Realistically-achievable cost on Bybit rank 31-150 perps is taken as 20-30 bps
round-trip (taker ~5.5 bps/side + slippage; a maker blend is optimistic and
adverse-selected). 40 bps gives headroom above that.

**Kill the lever.** OOS-negative at 10 bps on either OOS window.

**Expectation.** With the entry-lag fix the edge is bigger than the plan
assumed, so break-even cost is probably higher than the plan's "cost approx
edge" prior. Direction genuinely uncertain. Opposite result (negative at 10bps)
would mean the edge is structurally too thin and cost cannot save it.

## WS-2 — IC vs (entry-lag x hold-horizon) — PRE-REGISTRATION

**Hypotheses.** (A) IC decays with entry lag — the freshest executable entry
(1h) is best (confirms the WS-0d fix direction). (B) The 3d hold is inherited
arbitrarily; some horizon maximises raw IC and, separately, cost-adjusted edge.

**Method.** IS-train. IC of `reversion_score` and `z_rank_jump` vs forward
short return: (A) lag {1,6,12,24}h at 3d horizon; (B) horizon {1,2,3,5,7,10,14}d
at 1h lag. Plus a decile-spread net-return P&L proxy at 28.8 bps.

**Pre-registered pass.** Some horizon yields a decile-spread net return clearly
> 0 at 28.8 bps and not driven by a single regime. Horizon is chosen on
IS-train ONLY and then carried, unchanged, to validation/OOS.

**Expectation.** (A) monotone IC decay with lag — high confidence. (B) genuinely
uncertain: longer horizons give a bigger cross-sectional spread vs a fixed cost
(favours long), but reversion may fully decay by 7-14d (favours short). Opposite
of (A) — IC flat or rising with lag — would mean the entry-lag fix does not help
and WS-0d, while a correctness fix, is not a P&L lever.

---

## WS-1 / WS-3 — RESULTS

### WS-1 cost grid — oos_binance (corrected harness, continuous regime scaler)

| cost (bps) | stop fill | total_ret | sharpe | win |
|--:|--|--:|--:|--:|
| 10 | stop | −21.2% | +0.21 | 46.8% |
| 15 | stop | −28.6% | +0.15 | 46.7% |
| 20 | stop | −35.4% | +0.09 | 46.3% |
| 28.8 | stop | −45.8% | −0.01 | 46.0% |
| 48 | stop | −63.1% | −0.24 | 45.1% |
| 67 | stop | −74.8% | −0.46 | 44.3% |
| 10–67 | bar_extreme | −91% to −97% | −0.97 to −1.59 | — |

**Read.** Binance OOS is negative at EVERY cost, including an unachievable
10 bps (−21%, Sharpe +0.21 ≈ noise). The pre-registered WS-1 kill condition —
"OOS-negative even at 10 bps → cost is not the answer" — is **met on the
Binance OOS window**. Cost cannot rescue this window.

The `bar_extreme` adverse-fill stress is catastrophic at every cost (−91%+).
The strategy is wholly dependent on stop fills landing at the stop price, not
the bar extreme — the same fatal fragility the v1/v2 reports flagged. This is
the single most damning result so far and it is cost-independent.

**Caveat before declaring the cost lever dead globally.** This grid uses the
default *continuous* regime scaler, which still trades the 2021 alt-bull. The
hard regime gate (WS-3) is the a-priori fix for exactly that. WS-1's verdict is
held open until WS-3 + the other windows are in — but on the evidence so far,
cost is not the lever, and adverse-fill fragility may be terminal.

### WS-3 regime gate — oos_binance

| regime | cost/fill | total_ret | sharpe | win | trades |
|---|---|--:|--:|--:|--:|
| continuous | 28.8 stop | −45.8% | −0.01 | 46.0% | 1632 |
| **hard@−0.05** | **28.8 stop** | **−25.8%** | **+0.20** | 48.2% | 1274 |
| continuous | 48 stop | −63.1% | −0.24 | | 1632 |
| hard@−0.05 | 48 stop | −48.8% | −0.04 | | 1274 |
| continuous | 28.8 bar_extreme | −94.2% | −1.18 | | 1632 |
| hard@−0.05 | 28.8 bar_extreme | −90.4% | −1.00 | | 1274 |

**Read.** The hard regime gate genuinely helps — it drops ~360 alt-bull trades
and lifts Binance OOS −45.8% → −25.8%, Sharpe −0.01 → +0.20. But the window is
**still clearly negative**. From the cost slope (~−1.2 pp per +1 bp), the
hard-gate break-even cost on Binance OOS is ≈ **7 bps round-trip** — far below
any achievable execution (pre-registered achievable ≈ 20–30 bps; WS-1 pass bar
≈ 40 bps). And adverse fills remain catastrophic (−90%).

**Binance OOS verdict (3 of 4 windows still pending):** with the corrected
entry lag AND the a-priori hard regime gate, Binance OOS cannot be made
net-positive at any achievable cost, and is destroyed under adverse-fill
stress. For this window the cost lever is dead and the adverse-fill fragility
looks terminal. Held open pending Bybit-OOS / IS — but this is a strong
negative signal.

---

## WS-5 — capacity (oos_binance) + a structural insight

| AUM | pos notional | impact @C=0.5 | impact @C=1.0 |
|--:|--:|--:|--:|
| $0.25M | $50k | 0.10% | 0.20% |
| $1.0M | $200k | 0.20% | 0.41% |
| $2.0M | $400k | 0.29% | 0.58% |
| $5.0M | $1.0M | 0.46% | 0.91% |
| $10M | $2.0M | 0.64% | 1.29% |

Traded names are liquid — median signal-day $volume **$105M** (p25 $34M, p75
$295M); pumped alts have high volume on the pump day. Impact is modest: at $1M
AUM ~0.2-0.4%/trade. So liquidity/impact is NOT the binding constraint.

**The binding constraint is the edge itself.** Mean per-trade GROSS return on
Binance OOS = **−0.12%** (median −0.61%) — negative *before any cost*. Capacity
ceiling: N/A (no gross edge → no capacity at any size).

**Structural insight — alt-beta, not alpha failure.** This reconciles two
numbers that look contradictory: reversion_score IC is *positive* (+0.02 to
+0.04) yet the short trades' mean gross return is *negative*. Cross-sectional
IC measures *within-day rank* skill — "the top-decile names fall more than the
bottom-decile names". It says nothing about the LEVEL. The strategy is
**short-only**, so it carries the full alt-market direction: in an alt-bull
every alt rises and shorting any of them loses, even the "best" shorts. The
+0.04 IC is a thin cross-sectional tilt riding on top of a large, uncontrolled
net-short alt-beta. The regime gate (WS-3) is an attempt to time that beta; the
v2 report already proved the IS/OOS epoch divide is not a tradeable regime
signal, so beta-timing is structurally limited.

**Implication.** The honest cross-sectional edge (IC ~0.04) is only cleanly
harvestable **market-neutral** (short top decile AND long bottom decile, so
alt-beta cancels). Short-only imports an uncontrollable directional bet. WS-2's
`decile_spread_net` measures exactly that market-neutral spread — if it is
positive after cost on IS-train, the edge is real but mis-packaged as a
short-only strategy; if it too is ~0, there is simply no edge worth trading.
This is the sharpest open question for the verdict.

---

## WS-0c / WS-1 / WS-3 — Bybit OOS (2022-04 → 2023-05, the alt-bear window)

**Reproduction (WS-0c gate).** legacy entry (delay=24h) reproduces the report
EXACTLY: 681 trades, +33.7%, −41.2% DD, Sharpe 0.76. PASS.

**Entry-lag fix re-baseline.** fixed (delay=1h): 712 trades, **+244.8%**,
−28.1% DD, **Sharpe 2.13**, win 55.2%. The fix is worth +211 pp here — far
larger than on Binance (+32 pp). Mechanically sensible: in an alt-bear the
reversion right after the pump is fast and strong, so the 25h-late entry was
discarding the best part. The fix amplifies both windows; the bear window
benefits most.

**WS-1 cost grid (fixed harness):**

| cost | stop fill | total_ret | sharpe | | bar_extreme | sharpe |
|--:|--|--:|--:|--|--:|--:|
| 10 | stop | +310% | 2.39 | | +23.3% | 0.65 |
| 28.8 | stop | +245% | 2.13 | | +3.5% | 0.43 |
| 48 | stop | +189% | 1.87 | | −13.5% | 0.20 |
| 67 | stop | +142% | 1.61 | | −27.6% | −0.03 |

**WS-3 regime:** hard@−0.05 vs continuous on this all-bear window barely
differs (+255% vs +245%, Sharpe 2.40 vs 2.13, 581 vs 712 trades) — expected,
the gate has little to exclude when the whole window is alt-bear.

**Read.** With `stop` fills Bybit OOS is strongly positive at every cost — huge
cost headroom (break-even >> 67 bps). The cost lever's verdict is the exact
OPPOSITE of Binance OOS (negative at 10 bps). This is the v2 epoch-dependence,
reconfirmed: the strategy wins big in alt-bear, loses in alt-bull.

**The adverse-fill stress is the dominant risk and it bites every window.**
bar_extreme collapses Bybit OOS +245% → **+3.5%** at base cost, NEGATIVE at
48 bps. (Binance under bar_extreme: −90%.) The entire headline result depends
on stop fills landing at the stop price; the true result lies in the wide band
between `stop` and `bar_extreme`, and a strategy whose outcome swings from
+245% to +3.5% on a fill assumption cannot be sized with confidence. A 12% stop
on ~7%-daily-vol alts gets hit often, so stop-fill quality is make-or-break —
structural, not a tuning issue.

### Interim picture (2 of 4 windows + WS-1/3/5; WS-2/WS-4/IS pending)

- Entry-lag fix: a real, large lever — but it amplifies the existing
  epoch-dependence, it does not remove it.
- Both-OOS-windows viability (plan §6): Bybit OOS positive ✓, Binance OOS
  negative at any achievable cost ✗ → **viability criterion already failing.**
- Adverse-fill survival (plan §6): Binance −90%, Bybit +3.5%@base / negative at
  5× → effectively failing.
- WS-5 alt-beta diagnosis: the short-only packaging imports uncontrollable
  alt-beta; the thin cross-sectional edge (IC ~0.04) is the only real alpha.
- Verdict trend: NOT viable as a standalone short-only strategy. Pending WS-2
  (is the edge harvestable market-neutral?) and WS-4 (can features lift IC) and
  the IS windows before final.

---

## WS-2 — IC vs (lag × horizon) — OOS diagnostic (IS-train still pending)

Run on both OOS windows as a diagnostic of the WS-5 alt-beta question. Horizon
*selection* is reserved for IS-train per pre-registration; this is measurement.

**A. Lag-decay (3d horizon) — Hypothesis A CONFIRMED.** z_rank_jump IC:
- Binance: 0.041 (1h) → 0.037 (6h) → 0.030 (12h) → 0.028 (24h) — monotone decay.
- Bybit:   0.048 (1h) → 0.042 (6h) → 0.037 (12h) → 0.041 (24h).
IC decays with entry lag → the freshest executable entry is best → the WS-0d
entry-lag fix is a genuine P&L lever, not just a correctness fix.

**B. Horizon — Hypothesis B CONFIRMED (longer hold helps cost/edge).** The
market-neutral decile-spread net return (`(top−bot)/2 − 28.8bps`), z_rank_jump:

| horizon | Binance decile_net | Bybit decile_net |
|--:|--:|--:|
| 1d | −0.25% | −0.17% |
| 3d | −0.31% | −0.01% |
| 7d | −0.07% | +0.19% |
| 10d | −0.03% | +0.39% |
| 14d | −0.26% | +0.37% |

The cost-adjusted edge peaks at ~7–10d (a fixed cost amortised over a bigger
move), not the v1-inherited 3d.

**The decisive number — does the cross-sectional edge clear cost?** The
decile-spread is the alt-beta-free measure (short top decile / long bottom
decile). Gross spread = decile_net + 28.8bps:
- **Binance OOS:** gross spread peaks ≈ **+0.26%** per 10-day trade (z_rank_jump)
  — BELOW the 28.8 bps round-trip cost. Net is negative at EVERY horizon. Even
  market-neutral, even at the best horizon and freshest entry, the Binance
  epoch has no cross-sectional reversion edge that clears realistic cost.
- **Bybit OOS:** gross spread ≈ +0.68% per 10-day trade → net **+0.39%**. The
  bear epoch clears cost, thinly, at long horizons.

**This refutes the market-neutral salvage hypothesis (WS-5).** Removing the
alt-beta does not rescue the unfriendly epoch — the underlying cross-sectional
alpha there is genuinely sub-cost. The honest picture: gross edge ≈ realistic
cost (~25–30 bps); net edge ≈ 0 ± noise, slightly + in the bear epoch, slightly
− in the bull-containing epoch. Exactly the plan's §1 "IC ≈ 0.05, break-even
after cost" baseline — now confirmed with a corrected harness, the entry-lag
fix, AND the cleaner market-neutral measurement. The +245% Bybit short-only
backtest is alt-beta in a bear market, not a scalable alpha.

### Stress-testing the one good-looking number — Bybit OOS +245%

How hard I tried to break it (the user asked; here is the answer):

1. **Concentration in trades:** broad — top-10 trades = 12% of positive P&L,
   top-20 = 22%, across 712 trades, 55% win rate. NOT outlier-driven.
2. **Concentration in time:** heavy — monthly returns show 2022-04/05/06
   (+22%, +48%, +51%) compound to ~+170% of the +245%. That is the May–June
   2022 alt capitulation (LUNA / 3AC / Celsius cascade). The one losing-ish
   stretch, 2023-01 (−24%), is an alt relief rally.
3. **Adverse-fill (already shown):** +245% → +3.5% under bar_extreme.
4. **Alt-beta (already shown):** the market-neutral decile alpha is only
   ~+0.4%/trade; the rest of +245% is directional alt-short beta.

Conclusion: the +245% is overwhelmingly "short alt pumps through the biggest
alt crash in years" — a once-per-cycle beta event, fragile to fills, not a
repeatable scalable alpha. It does not survive scrutiny as evidence of edge.
The honest edge is the market-neutral decile spread (~+0.4%/trade, bear epoch;
negative, bull epoch) — i.e. ≈ break-even after cost.

---

## WS-4 — feature search — a real lead (OOS diagnostic; IS-train confirmation pending)

Standalone per-day cross-sectional IC of klines-derived candidates (the plan's
marquee funding/OI/taker features need datasets the rebuilt roots lack — an
explicit, documented gap). Top results, both OOS windows:

| feature | IC Bybit OOS | IC Binance OOS | rationale (a-priori) |
|---|--:|--:|---|
| **signal_day_range_pct** | **0.085** | **0.074** | blow-off intraday range = exhaustion |
| return_7d | 0.053 | 0.049 | cumulative run-up = more to revert |
| z_rank_jump (current best) | 0.048 | 0.041 | liquidity-rank migration |
| event_uniqueness_score | 0.027 | 0.029 | crowding |
| z_close_location | −0.009 | −0.001 | (dead — 4th confirmation) |

`signal_day_range_pct` — the signal day's intraday range (high−low)/close — has
~2× the IC of the current best component, t≈8–12 on EACH of two independent
windows (not a multiple-testing artifact), causal (signal-day data only), with
an a-priori-written exhaustion rationale. Its IC even RISES with horizon
(0.077→0.085 on Binance, 1d→14d) — coherent: a volatility blow-off reverts
slowly over many days.

**The decisive test — market-neutral decile spread net of 28.8 bps:**

| horizon | range_pct Binance | range_pct Bybit | z_rank_jump Binance |
|--:|--:|--:|--:|
| 3d | −0.43% | −0.07% | −0.31% |
| 7d | −0.16% | +0.10% | −0.07% |
| 10d | **+0.08%** | **+0.25%** | −0.03% |
| 14d | **+0.29%** | **+0.31%** | −0.26% |

`signal_day_range_pct`, traded market-neutral at a 10–14 day horizon, is the
FIRST signal with a positive net cross-sectional edge on BOTH OOS windows —
including the unfriendly Binance epoch where z_rank_jump never clears cost.

**This changes the verdict trajectory — honestly.** I had been drifting toward
a clean "kill"; running WS-4 before concluding (as the plan demands) caught it.
The current 4-feature short-only strategy is a poor parameterisation, not proof
the idea is dead. There IS a stronger feature.

**But measured, not hyped — the lead is THIN and caveated:**
- The edge is ~+0.1–0.3%/trade net — a low-Sharpe, ~+3–8%/yr market-neutral
  return at best. Thin.
- It sits right at the cost boundary. At a more realistic 40 bps for a 32-name
  market-neutral book (16 short + 16 long, longs in thinner names), the
  Binance 10d edge goes negative again. The 28.8 bps assumption is doing work.
- It is positive only MARKET-NEUTRAL and only at a LONG (10–14d) horizon — a
  packaging the current short-only harness does not implement.
- IS-train confirmation (the pre-registered selection window) is pending.
- Adverse-fill behaviour of a range-based signal (it shorts the highest-vol
  names → worse stop excursions) is untested.

WS-4 next, on IS-train: confirm `signal_day_range_pct` IC + decile_net; measure
the 4-window decile_net; short-only backtest as a cross-check.

### WS-4 diligence — decomposing the signal_day_range_pct lead (10d horizon)

Leg decomposition (does the edge live in the short leg, the long leg, or beta?):

**Binance OOS** — universe mean 10d short return −2.51% (alts rose: bull beta).
- short leg (short high-range): gross −2.47% net — loses, swamped by the bull beta.
- short-leg EXCESS vs universe +0.33%; long-leg excess +0.40% → genuine
  cross-sectional skill, balanced across both legs (not a one-sided artifact).
- decile_net +0.078% — but quarter-by-quarter: **+1.17%, −1.03%, +0.31%,
  −0.13%**. The "+0.08%" is a near-wash of ±1%/quarter swings.

**Bybit OOS** — universe mean +1.87% (alts fell: bear beta).
- short leg gross +2.50% net; the market-neutral edge is mostly in the SHORT
  leg here (short-leg excess +0.92%, long-leg +0.15%).
- decile_net +0.245%; quarters +0.60%, +0.61%, −0.23%, +0.005% — 3/4 positive,
  more stable than Binance but still one negative quarter.

**This tempers the WS-4 lead — honestly.** Two corrections to the headline:
1. The daily-IC t-stats (~10) are **autocorrelation-inflated** — daily ICs are
   not independent. At the quarterly-block level the decile edge is NOT
   statistically distinguishable from zero (Binance block-t ≈ 0.2, Bybit ≈ 1.2,
   n=4 — no power either way). The true edge is thin and its quarter-to-quarter
   sign-stability is poor, especially on Binance.
2. signal_day_range_pct is genuinely BETTER than the current features (its
   market-neutral excess is positive on both legs / both windows, vs
   z_rank_jump which was negative on Binance) — so the current 4-feature
   strategy IS a poor parameterisation. But even this better feature lands at
   ~break-even on the unfriendly epoch (+0.08%, unstable), not clearly positive.

So the WS-4 lead is real but modest: a better feature exists; it does not
rescue the unfriendly epoch; it confirms a thin, epoch-dependent, market-neutral
cross-sectional signal — not a deployable edge. IS-train confirmation pending.

---

## WS-2 / WS-4 — IS windows + THE decisive finding (IC ≠ P&L)

IS-train IC contradiction: `signal_day_range_pct` IC = +0.066–0.087 (t up to
10) on IS-train, yet its decile_net is NEGATIVE at every horizon (−0.34% to
−0.64%). IC and tradeable spread disagree → chased it with a full decile
profile (`research/ws4_decile_profile.py`).

**IS-train decile profile, signal_day_range_pct, 10d** (decile 9 = highest
range = the names the strategy shorts):

| decile | mean fwd | median fwd | win rate | p05 (left tail) |
|--:|--:|--:|--:|--:|
| 0 (lowest range) | −1.40% | +0.07% | 0.502 | −23.2% |
| 9 (highest range) | −1.63% | **+3.00%** | **0.568** | **−47.6%** |

`median` and `win_rate` rise monotonically with the signal — the highest-range
names DO revert more often (57% win rate, +3.0% median). That rank skill is
real and is what the +0.07 IC measures. **But the `mean` is flat-to-negative**
because the left tail blows out: the 5th-percentile forward short return for
the top decile is **−47.6%** — ~5% of the time the "exhausted pump" keeps
running and the short loses ~half. The catastrophic tail outweighs the +3%
median, so the MEAN — what a strategy actually earns — is −1.63%, worse than
decile 0.

### This is THE finding — and it explains the entire history of this strategy

1. **IC ≠ P&L.** Every IC in this study (~0.04–0.09) is *rank* skill. A short
   strategy earns the *mean*. Shorting the most extended/volatile pumps selects
   names with a genuine median edge AND a catastrophic left tail (pumps that
   keep running). The mean of (rank skill + fat left tail) is ≈ 0 to negative.
   The rank-IC systematically OVERSTATES tradeability. The mean-based
   `decile_net` is the honest measure — and it is the one that goes negative.

2. **The epoch-dependence is mechanical, not mysterious.** In an alt-bull epoch
   (IS-train 2023-24, Binance 2021) pumps continue more often/violently → the
   left tail is fatter → the mean of shorting them is negative. In an alt-bear
   epoch (Bybit OOS 2022, IS-valid 2024-26) pumps revert → the tail is benign →
   the mean is positive. v2 proved the epoch is not a tradeable signal; this is
   *why* — it is the short-side fat-tail asymmetry switching the mean's sign.

3. **The fat tail IS the adverse-fill catastrophe.** The −47% left-tail events
   are pumps continuing; a stop is meant to cap them but on a high-vol name the
   stop fills far through (bar_extreme −90%). The fat-tail problem and the
   adverse-fill problem are the same problem: shorting volatile names has an
   uncontrollable left tail.

4. **Every headline number is this asymmetry in a bear epoch** (v1 +2022%,
   rebuild +245% Bybit OOS) — alt-beta + benign-tail, not repeatable alpha.

### IS decile_net summary, signal_day_range_pct (best feature), market-neutral

| horizon | IS-train | IS-valid | Bybit OOS | Binance OOS |
|--:|--:|--:|--:|--:|
| 3d | −0.43% | −0.00% | −0.07% | −0.43% |
| 10d | −0.51% | +0.54% | +0.25% | +0.08% |
| 14d | −0.64% | +0.87% | +0.31% | +0.29% |

Even the strongest feature, market-neutral, at the best horizon: positive on
the bear-ish epochs, NEGATIVE on IS-train (a bull epoch). Epoch-dependent in
sign — the fat-tail asymmetry, not robust alpha. Not viable.

---

## WS-0c closed + WS-1/WS-3 IS windows + the entry-lag resolution

**WS-0c — all 4 windows reproduce (gate fully closed):**
- IS-train legacy: −28.53% vs reported −28.5%, 661 trades (exact). PASS.
- IS-valid legacy: +111.1% vs reported +115.4%, 1186 vs 1154 trades. PASS
  (within ±5pp/±5%; minor recent-archive drift).
- (Binance OOS, Bybit OOS reproduced exactly earlier.)

**WS-1/WS-3 IS windows (fixed harness, entry_delay=1):**

| window | 28.8 stop | 48 stop | 28.8 bar_extreme | hard-gate 28.8 stop |
|---|--:|--:|--:|--:|
| IS-train | −29.1% | −36.6% | −70.3% | −16.6% |
| IS-valid | −16.3% | −31.8% | −92.6% | −34.4% |

IS-train: negative at every cost incl. 10 bps (−20.8%). IS-valid fixed: −16.3%
(positive only at 10 bps stop, +2.1%). bar_extreme catastrophic on both
(−70%, −93%). The hard regime gate helps IS-train (−29%→−17%) but HURTS
IS-valid (−16%→−34%) — the gate is itself a Pareto knob.

**The entry-lag fix is epoch-dependent — contradiction resolved.** Effect of
the fix (legacy 25h → fixed 1h entry), total return at 28.8 bps:

| window | legacy | fixed | Δ |
|---|--:|--:|--:|
| Bybit OOS | +34% | +245% | **+211 pp** |
| Binance OOS | −77% | −46% | +31 pp |
| IS-train | −28.5% | −29.1% | ≈ 0 |
| IS-valid | +111% | −16% | **−127 pp** |

The fix HELPS in bear epochs and HURTS badly in IS-valid. Mechanism — the same
IC≠P&L fat-tail phenomenon: WS-2 lag-decay shows IC *rises* as entry gets
fresher on every window (better rank skill), but the freshest entry also walks
straight into the first-24h continuation window — where the fat left tail
(pumps that keep running) is fattest. The legacy 25h lag accidentally SKIPPED
that danger window. So fresher entry = higher IC but epoch-dependent mean P&L.

**Correction to WS-0d:** the entry-lag fix is a *correctness* fix (it matches
the documented "1h after close" intent; 25h was a date-convention bug) — but it
is NOT a clean P&L lever. It trades IS-valid against Bybit-OOS on the Pareto
frontier. (WS-0d already hedged "the fix could cut either way"; now the
direction is known — epoch-dependent.)

**The Pareto frontier, reconfirmed and now complete.** No single configuration
— across entry-lag, regime gate, cost, horizon, feature — is positive on all
four windows, or even on both OOS windows + IS-valid. Every knob trades windows
against each other. v2 proved this for gate-filtering; it holds for every lever
tested. The §6 viability criterion (positive on both OOS + IS-valid) is not
met by ANY configuration tried.

---

## VERDICT

Full verdict: **`docs/salvageability_verdict.md`**.

**Kill / shelve.** The short-only liquidity-migration strategy fails the plan's
§6 viability criteria on four independent pre-registered axes. The mechanical
reason: the signal has real cross-sectional *rank* skill (IC ~0.04–0.09) but a
short strategy earns the *mean*, and the mean is destroyed by a fat left tail
(~5% of shorted pumps continue and lose ~half). IC ≠ P&L. The tail's sign
tracks an untradeable regime; every lever (cost, horizon, regime gate,
entry-lag, stronger feature) trades one window for another with no interior
win. Do not deploy. Stop tuning. The bug fixes and IC tool are kept as genuine
repo improvements.
