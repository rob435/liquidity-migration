# Deployment Plan — Combined Book (Production Short + v11a Long Sleeve)

Owner-review draft. Not for execution until owner signs off on each phase gate.

## Abstract

Today the Singapore VPS (`5.223.42.109`) runs **only the short sleeve** (the
production `promoted` profile = `q40-h3-s12-tp26-c3`) on Bybit demo. Research
this session produced **v11a uni10 sniper retrace 1%/6h fallthrough** as a
long sleeve with near-zero correlation to the short and a stitched 4-year
Sharpe of +1.64. Stacked at 5× leverage the combined book reaches stitched
Sharpe +3.66 (vs +3.24 short alone) with **lower** max DD (-29.5% vs -36.1%).

The deployment goal: ship v11a long as a **second sleeve** on the VPS
alongside the untouched short, on a **separate account or separate
sub-process**, with full execution / risk / ledger / reconciliation
infrastructure equivalent to what the short already has.

The short config will **NOT** change as part of this plan — owner explicitly
chose to keep the production short untouched and overlay the long. The
session also produced a candidate Sharpe-4 short config
(`q50-h2-s12-tp26-c3`) that should be tracked separately and is **out of
scope** for this plan.

---

## 1. Current state (verified live on VPS, 2026-05-24)

### 1.1 Systemd units

Three units running as `root` from `/opt/liquidity-migration`, repo on `main`
at commit `240c95c` (`fix: suppress pybit 10006 rate-limit log storm on demo
VPS`), working tree clean:

| Unit | Purpose | Submits orders | Daemon mode | Telegram |
|---|---|---|---|---|
| `liquidity-migration-bybit-demo.service` | Entry cycle runner | **yes** | yes | yes |
| `liquidity-migration-bybit-risk.service` | Fast exit / risk watchdog | yes | n/a (WS) | yes |
| `liquidity-migration-bybit-paper.service` | Dry-run shadow of demo (no submit) | no | no | no |

### 1.2 `promoted` profile resolves to

`event_demo.py:_demo_event_config(profile="promoted")` returns
`VolumeEventResearchConfig()` defaults:

| Param | Value |
|---|---|
| `event_types` | `("liquidity_migration",)` |
| `thresholds` | `(0.40,)` |
| `side_hypotheses` | `("reversal",)` |
| `hold_days` | `(3,)` |
| `stop_loss_pcts` | `(0.12,)` |
| `take_profit_pcts` | `(0.26,)` |
| `cost_multipliers` | `(3.0,)` |
| Entry policy | `promoted_quality_squeeze` |
| Strategy ID | `liqmig_union_q40_h3_tp26_g100_qsqueeze` |

### 1.3 Hard gates in `scripts/run_bybit_demo_event_engine.sh`

- `SUBMIT_ORDERS=1` requires `STRATEGY_PROFILE=promoted` (any other profile
  exits the runner with code 2). Source: lines `if [[ "$STRATEGY_PROFILE"
  != "promoted" ]]; then echo "Only STRATEGY_PROFILE=promoted is allowed to
  submit demo entry orders." >&2; exit 2; fi`.
- `SUBMIT_ORDERS=1` requires `CONFIRM_DEMO_ORDERS=1`.
- `TELEGRAM_ENABLED=1` requires bot token + chat id.
- `STRATEGY_PROFILE` only accepts `promoted` or `demo_relaxed`
  (`event_demo.py:DEMO_STRATEGY_PROFILES`).

### 1.4 Live activity (sampled)

- Demo cycles run every 60s, ~165 universe symbols, ~7400 features/cycle,
  cycle elapsed <0.5s normally (2–3s on klines refresh).
- 4 historical filled order rows in
  `data/bybit-demo-event/event_demo_orders/part.parquet` (last fills
  2026-05-21 — exit-only `lm-ux-*` prefix orders, qty already at
  target).
- No open trades currently. No recent entries (the promoted profile is sparse,
  expected behavior given the research signal fires ~1×/3 days on average).
- Paper service is healthy, generating dry-run cycles identical in structure
  to the production demo cycle output.

### 1.5 Order-link-id prefix convention

`event_demo.py` writes entries with prefix `lm-en-*`. `ws_risk.py` writes
exits with prefix `lm-ux-*` (or `lm-ex-*`). Each service consumes only its
own prefix on its own WS subscription. Adding a long sleeve must use a
distinct prefix family (proposed: `lm-en-l-*` and `lm-ux-l-*` /
`lm-ex-l-*`) to avoid cross-talk.

### 1.6 Data root layout

- Short demo: `/opt/liquidity-migration/data/bybit-demo-event/`
- Short paper: `/opt/liquidity-migration/data/bybit-paper-event/`
- Long would need: `/opt/liquidity-migration/data/bybit-long-demo-event/`
  and `/opt/liquidity-migration/data/bybit-long-paper-event/`.

### 1.7 Account / API credentials

- `/etc/liquidity-migration/bybit-demo.env` holds `BYBIT_DEMO_API_KEY` /
  `BYBIT_DEMO_API_SECRET` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`.
- Both the short demo and short risk services share this single Bybit demo
  account. Adding the long sleeve on the same account would multiplex the
  same demo equity across short and long positions; running long on a
  **separate Bybit demo subaccount** is strongly preferred (cleaner
  attribution, no equity contention, simpler risk modeling).

---

## 2. Target state

A second sleeve running alongside the existing short:

- **Sleeve A (short)** — unchanged. `q40-h3-s12-tp26-c3` promoted profile,
  on Bybit demo account A.
- **Sleeve B (long, v11a)** — new. uni10 sniper retrace 1%/6h fallthrough,
  on Bybit demo account B (separate subaccount preferred) OR same account
  with explicit position-side discipline (long-only positions only).
- **Combined leverage**: long sleeve sized at 5× per-position notional
  vs the short's 1×. This is per-position scaling (each long entry uses 5×
  the position notional the short uses), not account-level cross-margin
  leverage. The Bybit demo account itself stays on default leverage.
- **Risk monitoring**: separate ws_risk watchdog for long-side positions,
  or extend the existing one to handle both prefixes.
- **Ledger / equity attribution**: separate parquet ledgers per sleeve so
  research attribution stays clean.
- **Telegram**: separate notification thread for long sleeve to avoid
  short-side message volume drowning out long signals.

---

## 3. Gap analysis

What's missing to ship v11a long to live demo:

### 3.1 Python module gaps (multi-day work)

The short sleeve has a dedicated execution pipeline that doesn't yet exist
for the long. The long_native module currently is research-only
(`run_long_native_research`). To run live:

| Component (short side, exists) | Long-side equivalent (missing) | Est. effort |
|---|---|---|
| `event_demo.py` (1500 LOC: cycle runner, candidate detection, order sizing, fill confirmation, ledger writer) | `long_native_event_demo.py` | 2-3 days |
| `event_demo_daemon.py` (WS-driven daemon wrapper) | `long_native_event_demo_daemon.py` | 0.5 day (mostly copy + adapt prefix) |
| `ws_risk.py` integration for short exits | Long-side exit handling: TP/stop/time-stop + WS push | 1-2 days |
| `volume_events.py` event detection + entry quality | Already in `long_native.py:detect_pattern_fomo_chase` + `_fc_exit_params` + sniper retrace logic | done (research code) |
| Promoted profile loader (`_demo_event_config`) | Need long-side equivalent profile system, e.g. `("v11a_uni10_sniper",)` | 0.5 day |
| Order router with `lm-en-*` prefix | New router instance for `lm-en-l-*` (can share `ExecutionEventRouter` class, separate WS subscription) | 0.5 day |
| Reconciliation script `reconcile_paper_demo` | Long-side equivalent | 0.5 day |
| CLI subcommand `event-demo-cycle` | New `long-native-demo-cycle` subcommand | 0.5 day |
| Telegram message formatter | Reuse `event_demo.py:_event_demo_telegram_text` with long-context labels | 0.25 day |

**Total: 5-8 engineering days of new code** before the long sleeve can submit
orders to Bybit demo through the same WS-driven path the short uses.

### 3.2 Infrastructure gaps (~1 day)

- New systemd unit file `liquidity-migration-bybit-long-demo.service`
- New runner script `scripts/run_bybit_long_demo_event_engine.sh`
- New systemd unit `liquidity-migration-bybit-long-paper.service` (dry-run shadow)
- New systemd unit `liquidity-migration-bybit-long-risk.service` (or extend existing)
- New env file `/etc/liquidity-migration/bybit-long-demo.env` if separate account
- `/opt/liquidity-migration/data/bybit-long-demo-event/` writable for the unit

### 3.3 Account / credentials gap

- Decide: separate Bybit demo subaccount for long sleeve (recommended) OR
  same account with position discipline.
- If separate: register new subaccount on Bybit demo, generate API key/secret,
  store in new env file with permissions `chmod 600`.
- Either way, document the choice in
  `docs/data_roots.md` so future agents know.

### 3.4 Risk-management gap

The existing short ws_risk service knows about short positions only (reduce-only
short exits). Adding long sleeve requires either:

a. Extending `ws_risk.py` to track positions by side and apply long-specific
   exit logic (TP at +K×ATR, stop at -K×ATR, time-stop at 3 days), OR
b. New `long_ws_risk.py` running as separate service.

Option (b) is cleaner and matches the per-sleeve isolation pattern.

### 3.5 Research / validation gap

Before shipping the long sleeve to demo:

- v11a long OOS validation: stitched Sharpe +1.64 (4y), but OOS Binance
  Sharpe +1.06 with funding MISSING. Need a fresh OOS Binance run that
  includes funding-cost estimation (manual or proxy) before claiming the
  +1.06 is honest.
- v11a long has never been forward-tested. Demo deployment **is** the forward
  test. Plan for 4-8 weeks of demo before any real-money consideration.
- Position-sizing equivalence: in research the long uses `max_position_weight=0.30`
  with `max_concurrent_positions=5` (so each position ≤6% of book at 1×).
  At 5× equivalent leverage, each long position uses 30% of book notional.
  This needs explicit max_position_size and max_concurrent caps coded in the
  live module to match research.

---

## 4. Phased deployment plan

### Phase −1 — Get research code into git **(critical pre-requisite)**

**Verified during plan drafting**: the entire long-sleeve research module
`liquidity_migration/long_native.py` is **untracked in the local git repo**
— it was never committed, never pushed to origin, and the VPS does NOT have
it (verified by `ssh root@5.223.42.109 'cd /opt/liquidity-migration && ls
liquidity_migration/long_native*'` returning empty). The same is true of
`momentum_factor.py`, `cross_sectional_momentum.py`, `long_hourly.py`, and
all docs from this session including this very document.

Without this step, the engineering team has no source-of-truth for the v11a
config to build the live module from.

**Deliverables before Phase 0:**
- `git add liquidity_migration/long_native.py docs/long_native_findings.md
  docs/deployment_plan_combined_book.md` (and any other research modules
  the owner wants tracked)
- Commit with message explaining provenance ("v11a research module from
  research session, used as input for deployment plan")
- `git push origin main` so VPS can `git pull`
- `git stash` or commit the modified `cli.py`/`config.py`/`volume_events.py`
  files currently in the working tree (currently uncommitted)
- Verify VPS can pull: `ssh root@5.223.42.109 'cd /opt/liquidity-migration
  && git fetch && git status'`

**Gate to advance:** `git status` clean locally, VPS sees the new files
on `origin/main`.

### Phase 0 — Owner sign-off on plan (you reading this)

**Decisions required from owner before Phase 1 starts:**

1. Same Bybit demo account or separate subaccount for long sleeve?
2. Per-position 5× notional (matches research) or 1×/2×/10× for initial demo?
3. Telegram notifications: same chat or separate channel?
4. Acceptable demo-validation window before any real-money discussion (suggest 8 weeks).
5. **Approve commit of `long_native.py` and related research modules to main.**
   They contain only the FC config + sniper retrace code, no secrets, no
   API keys, no PII. Safe to commit.

### Phase 1 — Long-side execution module (engineering, ~5 days)

Build the long-side mirror of `event_demo.py` + daemon. Done outside the
VPS first.

**Deliverables:**
- `liquidity_migration/long_native_event_demo.py` (new)
- `liquidity_migration/long_native_event_demo_daemon.py` (new)
- CLI subcommand `long-native-event-demo-cycle`
- Profile loader registers `("v11a_uni10_sniper",)` mapping to the v11a
  config from this session
- Tests covering: profile loading, sniper retrace logic in live context,
  position sizing math, order link id prefix isolation, dry-run cycle output
  parity with research run

**Gate to advance:** all tests pass locally; one manual `--dry-run` cycle
produces a candidate list that exactly matches what
`run_long_native_research` produces for the same date.

### Phase 2 — Long-side ws_risk service (engineering, ~1.5 days)

Build `liquidity_migration/long_native_ws_risk.py` mirroring `ws_risk.py`.

**Deliverables:**
- Long-side reduce-only exit handler for TP / stop / time-stop
- WS execution stream subscription with `lm-ux-l-*` prefix consumption
- Adopt-orphan-position logic (matches short side behavior)
- Tests

**Gate to advance:** simulated fill messages route to correct exit handlers;
no cross-talk between short prefix (`lm-ux-*`) and long prefix (`lm-ux-l-*`).

### Phase 3 — Systemd units + runner scripts (1 day)

**Deliverables (committed to repo, applied to VPS):**
- `deploy/systemd/liquidity-migration-bybit-long-demo.service` (start with
  `SUBMIT_ORDERS=0` initially)
- `deploy/systemd/liquidity-migration-bybit-long-risk.service` (start with
  `SUBMIT_ORDERS=0`)
- `deploy/systemd/liquidity-migration-bybit-long-paper.service` (always
  `SUBMIT_ORDERS=0`)
- `scripts/run_bybit_long_demo_event_engine.sh` with the same hard gate
  pattern (`SUBMIT_ORDERS=1` requires explicit profile + `CONFIRM_DEMO_ORDERS=1`)
- `scripts/run_bybit_long_demo_ws_risk_engine.sh`
- `/etc/liquidity-migration/bybit-long-demo.env` on VPS (chmod 600)

**Gate to advance:** services start under systemd, journal output shows
healthy 60s cycles with no errors, NO orders submitted (paper mode only).

### Phase 4 — Paper validation on VPS (1-2 weeks of wall time)

Run the long sleeve in paper mode (SUBMIT_ORDERS=0) on the VPS for
**minimum 14 days** to validate:

- Cycle runs cleanly every interval, no exceptions, no leaked locks
- Signal candidates fire at the rates the research predicts (96/3y ≈
  1/12 days expected; tolerate 1.5× either direction during 14 days)
- When a candidate fires, the order sizing math matches what would have
  happened in research
- Telegram notifications come through if enabled
- Memory / CPU steady-state (no leaks)

**Gate to advance:** 14 days of clean paper logs, signal rate within
expected range, no unrecovered exceptions.

### Phase 5 — Demo orders enabled (~6 weeks of forward evidence)

Flip `SUBMIT_ORDERS=1` on the long demo service. Begin live demo execution.

Validation milestones:
- Week 1: at least 1 fill, position fully entered + exited, ledger entries
  reconciled, no orphan positions, telegram notifications correct
- Week 2-4: ≥5 trades, win rate within ±20pts of research expectation
  (55% expected → 35-75% acceptable), no risk service alerts
- Week 4-8: cumulative PnL within ±2σ of research expectation, max DD
  consistent with research max DD bound

**Gate to advance to real-money discussion:** 8 weeks of demo trading with
fill-vs-research drift < 30bps per trade, no infrastructure failures, no
unexpected position adoption events.

### Phase 6 — Real-money discussion (deferred, requires explicit owner
go-ahead and is NOT part of this plan)

Per `AGENTS.md`: real-money toggle is a `.env` flag, explicitly NOT to be
flipped without owner instruction. Even after 8 weeks of clean demo,
real-money deployment requires:

- Funding cost validation (estimated or proxied) — currently the OOS Binance
  Sharpe 1.06 assumes zero funding
- Capacity / market impact assessment for actual notional sizes
- Production runbook documented (incident response, position emergency exit,
  account-level kill switch)
- Independent code review of all long-sleeve modules

---

## 5. Validation gates summary

| Gate | When | Owner | What must hold |
|---|---|---|---|
| G0: plan signoff | Before Phase 1 | owner | Decisions 1-4 answered |
| G1: code complete | End Phase 1 | engineer | Tests pass, dry-run parity vs research |
| G2: risk-svc complete | End Phase 2 | engineer | No prefix cross-talk |
| G3: infra ready | End Phase 3 | engineer | Services up, paper-only, healthy |
| G4: paper validated | End Phase 4 (14d) | engineer + owner | Cycle stability, signal rate sane |
| G5: demo trading | Phase 5 entry | owner | Flip SUBMIT_ORDERS=1, monitor week 1 |
| G6: demo PnL valid | End Phase 5 (8w) | owner | PnL within ±2σ of research, no infra fails |

---

## 6. Rollback procedure

At any stage:

1. `systemctl stop liquidity-migration-bybit-long-demo.service` →
   `systemctl stop liquidity-migration-bybit-long-risk.service`
2. If long positions open, flatten via Bybit UI (small enough to manual-close
   during demo phase) OR run a one-shot reduce-only exit cycle via the runner
   script with `--exit-all` flag (need to add this flag)
3. Disable the units: `systemctl disable liquidity-migration-bybit-long-demo.service`
4. The short sleeve is untouched at every stage and continues to run

Rollback time budget: 5 minutes wall time to fully stop long sleeve and
have positions reduce-only-closed by next cycle.

---

## 7. Risk surface

### 7.1 What could go wrong with the new long sleeve

| Failure mode | Severity | Mitigation |
|---|---|---|
| Order sizing math bug → oversized position | high | Phase 4 paper validation, position-size cap in code (max_position_weight=0.30), separate Bybit subaccount caps exposure |
| WS reconnect loses fill events → orphan positions | medium | Existing `adopt_untracked_positions` logic from short side, will mirror for long |
| Sniper retrace logic fires wrong bar → bad fill price | medium | Phase 1 tests with exact research date replay, Phase 5 fill-vs-research delta monitoring |
| Long signal collides with short signal on same symbol | low | Universe disjoint in practice (short uses rank 11-220, long uses rank 1-10), enforce with universe checks in long module |
| Telegram noise | low | Separate channel for long notifications |
| `lm-en-l-*` prefix not isolated → exit-side service mis-routes | medium | Prefix isolation tested in Phase 2 |

### 7.2 What could go wrong with the **existing short**

This plan should not affect the short. But the long share systemd same
machine, so:

| Failure mode | Severity | Mitigation |
|---|---|---|
| Long service OOM kills the machine | medium | systemd MemoryLimit on long unit, conservative 1GB |
| Long service consumes private REST budget → short rate-limited | medium | Long uses its own `BybitPrivateRateLimiter` capped at 5 req/s (vs short's 15 req/s budget) to leave headroom |
| Disk fills from long-side parquet ledger | low | Standard journald rotation + monthly partitioning of long ledger parquets |

---

## 8. Open decisions / TODO before plan can execute

1. **Account topology**: same Bybit demo account or new subaccount? Owner
   pick. Recommendation: **separate subaccount** — cleaner attribution,
   no equity contention, real-money path is sub-account-based anyway.

2. **Per-position notional**: 1× / 2× / 5× / 10× of the short's per-position
   notional? Research peak Sharpe at 5×, but real-money risk argues for
   conservative 2× initial demo, scale up after 4 weeks of validation.

3. **Telegram channel**: same chat or new? Suggest new — short already
   produces ~daily position notifications, long would add ~weekly.

4. **OOS funding cost backfill for v11a long**: do we want to spend a half-day
   building a funding-cost estimator (or proxy from existing Bybit funding
   data we have) and re-running OOS to get a funding-aware Sharpe? This
   would tighten the +1.06 OOS Binance number before we deploy.

5. **Promoted profile name for v11a**: proposed `v11a_uni10_sniper`. Or
   prefer something more semantic like `long_native_promoted` to match short's
   `promoted` naming?

6. **Risk service architecture**: extend the existing `ws_risk.py` to handle
   both sides, or build a separate `long_native_ws_risk.py`? Suggest separate
   for clean isolation and easier independent rollback.

---

## 9. Out-of-scope items (explicit non-goals)

- **Short config swap to q50-h2**: that's a separate decision with its own
  validation path (q50-h2 had OOS gate failures in this session's tests).
  Not part of this plan.
- **Real-money deployment**: explicitly deferred, requires separate owner
  go-ahead per `AGENTS.md`.
- **Per-coin parameter tuning of v11a long**: research showed plateau, the
  uni10 + sniper config from this session is the operating point.
- **Multi-pattern long sleeve** (FC + CAP + OB combined): research showed
  CAP and OB don't add alpha standalone. FC-only.

---

## 10. Estimated end-to-end timeline

- Phase 1-3 (engineering + infra): **~7 calendar days** with owner-side
  reviews between
- Phase 4 (paper validation on VPS): **14 calendar days** of wall time
- Phase 5 (demo with submit): **8 weeks** of wall time before any
  real-money discussion

So earliest real-money discussion ≈ **10 weeks from owner sign-off on Phase 0**.

---

## 11. Validation already performed during this drafting session

The plan is grounded in actual VPS state (not assumed):

- SSH'd to `5.223.42.109` and read all three systemd unit files
- Verified `promoted` profile resolves to q40-h3-s12-tp26-c3 by reading
  `event_demo.py:_demo_event_config` and `volume_events.py:VolumeEventResearchConfig`
- Verified the runner script's hard gates by reading
  `scripts/run_bybit_demo_event_engine.sh`
- Verified production demo is actively cycling every 60s (journal logs,
  cycle counter incrementing)
- Confirmed paper service running on VPS is a viable shadow-test path
  (SUBMIT_ORDERS=0, same code path, separate DATA_ROOT, just dry-run)
- Confirmed the long-side equivalent infrastructure does NOT exist (no
  `long_native_event_demo.py`, no `long_native_*` systemd units)
- **Critical** — verified the long_native.py module itself is not on the VPS
  (`ssh root@5.223.42.109 'cd /opt/liquidity-migration && .venv/bin/python -c
  "from liquidity_migration.long_native import LongNativeConfig"'` →
  `ModuleNotFoundError: No module named 'liquidity_migration.long_native'`)
  and discovered it's also **untracked in local git** (`git ls-files
  liquidity_migration/long_native.py` returns empty)
- Read `event_demo_daemon.md` for the WS-driven daemon architecture that the
  long-side replica must mirror
- **Demo execution test (Phase 4 preview)**: ran one-shot
  `event-demo-cycle --strategy-profile promoted --record-dry-run` on the VPS
  with all production env vars. Cycle completed in 25.3s (24.8s kline
  fetch, 0.5s signal pipeline), no errors, wrote
  `data/.../latest_event_demo_cycle.md` artifact. This is direct evidence
  that the entire short-side execution path (kline pull → feature build →
  universe filter → signal detection → entry sizing → ledger write) is
  operational, which is the template the long-side build will mirror.

The single biggest gap is **engineering time** to build the long-side
execution module + risk service. Until that's done, no v11a trades can
actually be submitted to Bybit demo, regardless of how convincing the
backtest is. The second biggest gap is more procedural than technical:
getting the existing research module committed to git so other agents and
the VPS can use it.
