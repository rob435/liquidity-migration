# Continuous-fade system — fault audit & improvement roadmap

**Date:** 2026-06-02. **Method:** a 7-dimension adversarial fault audit (62 agents, every claim
verified against the code/docs); 48 confirmed faults deduped to the distinct issues below. EXPLORATORY
forward-demo sleeve — demo/paper only, never real money. **Nothing here is pushed — operator deploys.**

The continuous system's *signal* is real (all-weather, cross-venue, cross-sectional — NOT a short-beta
tailwind: the engine's L/S decomposition put the short book's market beta at only −0.10/−0.20). The
faults are overwhelmingly in **fidelity, risk machinery, observability, and strategic fit**, not the
alpha — consistent with the program's own STR-factor finding ("the sophistication that helped was risk
machinery, not new signal").

---

## 1. FIXED this session (safe, non-signal-changing — done + test-gated, suite 1086 pass)

1. **[CRITICAL] rmom silent-signal blackout.** `precompute_residual_momentum.py` hardcoded
   `END="2026-05-28"`; the daily systemd refresh re-ran with the same cap, so it could never write a row
   for "today". The live decile join (`...is_not_null()`) then drops the ENTIRE cross-section → zero
   entries, *silently* (`rmom_present=True` because the file exists; `live_d9_symbols=0` reads as a quiet
   market). Today (2026-06-02) is past the cap → the sleeve would emit no signal the moment it is armed.
   **Fix:** `--end` now defaults to **tomorrow (UTC)** (PIT-safe: `residual_return[d]` completes at d+1 +
   the lag1 `shift(1)` already exclude the signal day), the parquet write is now atomic (temp+rename),
   and the false service/doc comments are corrected.
2. **rmom freshness telemetry + watchdog guard.** The cycle now persists `max_rmom_day_ts` /
   `rmom_stale_days` (not just the misleading `rmom_present` boolean), and `check_demo_liveness.py` now
   monitors the continuous sleeve directly — per-sleeve **cycle-age**, **rmom staleness** (pages if the
   newest residual day is > 2d old = the blackout class), and **server-side stop protection** on its own
   open trades (it previously checked only the systemd-failed state).
3. **Reactivity-loop observability.** The daemon's fast-loop counters are persisted as `rx_*` in the
   cycle (protective-loop alive/checks/exits/errors/last-check-age, ticker-batch wakes, fill nudges) so a
   silently-dead protective-exit thread is visible instead of invisible.
4. **Portfolio circuit breaker — VALIDATED 2026-06-02; not a MAR win, ENABLED on live as tail insurance.**
   `entry_circuit_breaker_tripped` PAUSES new entries when `entry_pause_after_adverse_exits` adverse
   covers land within `entry_pause_window_minutes`. Stateless, never adds risk. Engine-validated both
   venues (`docs/preregistration/cb1-circuit-breaker-2026-06-02.md`, `scripts/cb1_circuit_breaker_validate.py`):
   it robustly helps the *squeezier* book (binance, baseline DD 5.1% — whole w24 column beats OFF) but
   **hurts the already-clean bybit book (baseline DD 2.6%): off is optimal, 18/19 cells lose MAR; the one
   winner, w24/n8, is a lone noise spike (neighbors degrade MAR 13–25%).** Reliably trades return for DD.
   **Operator-directed 2026-06-02: ENABLED on the live sleeve at w24/n8** (`entry_pause_after_adverse_exits=8`,
   `entry_pause_window_minutes=1440`) as deliberate protective *tail insurance* — accepting ≈ −21% bybit
   return for −27% DD in-sample; not a profit tuning. Engine default stays OFF. Disable via
   `entry_pause_after_adverse_exits=0`.

---

## 2. Decisive experiments — RUN THESE FIRST (engine-only, safe, no live change)

These are cheap (the engine exists), change the deploy decision the most, and gate the operator-decisions
in §3. Pre-register each (`docs/preregistration/`).

- **G1 — the 3-way redundancy backtest (THE decision).** deployed short + existing long sleeve +
  continuous **L/S overlay**, measured together. The research's own conclusion is that the continuous
  SHORT-only does NOT beat the daily on MAR and the value-add is a market-neutral L/S overlay — but that
  overlay may be **redundant** with the existing long sleeve (corr ~+0.3 to the short vs the long's
  ~−0.03). Until this is run, the continuous program's *entire deployable thesis is unverified*. If
  redundant → the program is a research result, not a sleeve. If additive → §3-A becomes the priority.
- **G2 — realistic engine at `entry_delay_hours=0`.** The live sleeve trades the 0h operating point
  (immediate decile-cross entry); the engine only ever validated +1h (0h was the look-ahead proxy). 0h on
  the *live* price is causal (not look-ahead), so it deserves a true measurement — does shorting AT the
  extreme beat entering 1h into the fade, or does it walk into the squeeze? (Bar-based engine caveat: the
  sub-hourly live entry has no perfectly-faithful backtest; 0h is the closest bound.)
- **G3 — net MTM effect of the live reactivity machinery in the engine.** stop_approach / hysteresis
  (`exit_decile_buffer`) / re-entry cooldown change live exit/entry timing with ZERO engine validation.
  Ablate each against MTM-MAR/DD; default the losers OFF.
- **G4 — borrow / short-availability** (audit #2 un-closable risk, likely a chunk of the implausible
  residual Sharpe ~10): add even a crude borrow-cost term to the engine's friction waterfall and re-rank.
- **G5 — impact calibration + capacity:** log realized entry/exit slippage live (fill vs decision price),
  fit the modeled `impact_coef_bps`/`exponent` from the demo ledger, and sweep capacity at deploy size —
  today the coefficients are guesses and capacity is asserted, not measured.

---

## 3. Operator-gated (changes the live profile / signal — recommendations)

- **A. Forward-test the L/S overlay, not short-only.** *If G1 says additive*, the live sleeve should
  short D9 **and** long D0 (beta-neutral) — that is the actual deliverable. Running short-only is
  defensible ONLY as an explicit, time-boxed stepping stone to validate the short leg's live frictions
  (borrow, squeeze, fills); make that intent explicit or switch.
- **B. Entry-timing confirmation.** Per G2, consider a small confirmation delay (or an N-bar
  down-confirmation) so the live operating point matches a *validated* one rather than the un-measured 0h.
- **C. stop_approach default.** Per G3 — if it doesn't improve MTM-MAR, flip `stop_approach_frac=0` (rely
  on the 25% server stop + the §1.4 breaker). It is currently ON (0.8) and unvalidated.
- **D. 25% disaster-stop width.** The engine showed a 25% stop *hurts* return; it is the operator's
  I-phase cap, not a validated value. Revisit alongside the breaker (a portfolio breaker may let the
  per-trade stop be wider/cheaper).
- **E. Sizing.** Flat 2% gives the wildest pumped alt the most dollar risk. Inverse-vol was rejected
  (DD-up), but a *clamped per-name dollar-risk cap* or a regime-tightening gross cap was never tested.
- **F. `liq_turnover_min=$500k`** sits on the liquidity cliff where the edge is liquidity-monotone — a
  threshold on its own sensitivity edge. Sweep it; pick a value off the cliff.
- ~~**Enable the §1.4 circuit breaker** once validated~~ — **DONE / resolved 2026-06-02:** validated in
  the engine, NOT beneficial on the live (Bybit) book, left OFF (see §1.4 + the cb1 receipt). The
  remaining squeeze-tail defenses on bybit are the per-trade stop_approach + the 25% server stop; a
  *portfolio* breaker only pays on a squeezier book than bybit's in-sample 2.6%-DD profile.

---

## 4. Smaller safe improvements (low-effort; do opportunistically)

- **Held-name protective coverage gap:** when a held name rotates OUT of the top-250 WS-kline universe,
  the fast loop's MFE→0 silently disables *breakeven* + *failed_fade* for it (stop_approach + the 25%
  server stop still protect). Fix by explicitly subscribing all *held* symbols to the kline stream.
- **Productionise the MTM-drawdown metric:** the hourly/intraday MTM that produced the headline 3× DD
  correction lives in an ad-hoc `/tmp` script, not in `continuous_events.py` — fold it in so it can't rot.
- **Dead config:** `max_concurrent_entries=4` is never used on the continuous sleeve (no parallel-submit
  path) — remove or wire it.
- **Top-250 vs full-universe decile divergence** (G/N): the live decile + rmom gate rank within the
  top-250-by-*current*-turnover universe (survivorship-tilted) while the backtest uses the full PIT
  universe — measure D9-membership divergence; raise the cap if material.
- **Binance data ends 2026-04-30** (~1mo short of the 2026-05-28 config end) — the binance "recent /
  all-weather" verdict rests on a shorter window than bybit's; refresh before citing it.

---

## 5. Considered and explicitly NOT changed (with reasons)

- **Throttling the exit-order burst during a squeeze (a flagged "fault") — REJECTED.** A correlated
  squeeze firing many simultaneous *protective covers* is the system working; rate-limiting *exits* would
  delay safety covers — the wrong direction. The correct mitigation is the *entry-side* breaker (§1.4) +
  accepting the cover burst; protective exits must never be throttled.
- **The age-floor "can't exclude <30d listings" claim — NOT a fault.** The 45-day kline window's earliest
  bar for a <30d listing IS its listing, so `(now − first) < 30d` correctly excludes it; symbols older
  than the window floor to ~45d ≥ 30d and correctly pass. Verified.
- **"Short edge is mostly short-beta" hypothesis — REFUTED by the engine** (L/S beta −0.04/−0.07; short
  beta only −0.10/−0.20). The edge is cross-sectional. Regime-conditionality (recent-tilted) remains a
  real caveat, but it is not a hidden beta exposure.
