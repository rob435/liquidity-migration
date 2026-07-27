# 2026-07-27 VPS fleet + notification audit

Scope: answer "what is wrong" behind the Telegram alert traffic, audit the
fleet two days after the CONTINUOUS `single_fund0` deployment (`d16daf5`),
and map operator-notification coverage. Method: five parallel auditors over
the local repo plus read-only SSH evidence, each significant finding
independently re-verified against primary sources. All times UTC.

## What the operator saw in Telegram

The recurring alert/resolved pairs are the liveness watchdog's
`account_capture_stale`: "account execution live L2 is ~5 min stale (> 3
min); owner may be hung or disconnected", each self-resolving ~3 minutes
later. Eight events since Jul 20 (04:07 Jul 20, 03:35 + 20:05 Jul 21, 05:53
Jul 22, 06:19 Jul 24, 21:00 Jul 26, 02:28 + 08:24 Jul 27) — pre-existing,
not caused by the Jul 26 deployment (capture/liveness files have an empty
diff at `d16daf5`; the signature is identical before and after), but
frequency rose to 3 in the last 12 hours.

## Root cause (verified)

The demo owner's raw public-linear websocket (`BybitRawPublicMarketStream`,
one `orderbook.50.BTCUSDT` subscription — the idle-heartbeat symbol)
genuinely stops delivering frames for ~5–8 minutes. The alert is a true
positive for a real L2 gap; execution stays safe because stale books fail
closed (`RequestedMarketWarmupGate` / `max_market_age_ns`) and REST
reconciliation stays healthy throughout. Three compounding blind spots made
it undiagnosable from journals:

1. `WebSocketApp` was built without `on_error`/`on_close`;
   websocket-client 1.9.0 routes every transport failure through those
   (absent) callbacks and returns from `run_forever` without raising, so the
   stream's only ERROR log line was dead code. Zero journal lines existed in
   any stall window.
2. The internal stale-subscription watchdog wipes its per-symbol evidence on
   every detach/reconnect attempt, so a fast-fail reconnect loop never
   crosses a threshold; its WARNING has fired zero times since Jul 20.
3. No in-process staleness self-check: the 1 Hz readiness sidecar is written
   only from the frame path and read only by the external 3-minute timer.

Contributing condition (verified separately): the 2-vCPU host is saturated —
load ~5–6, PSI cpu-some 59%, ~1 GB swap in use. Plausible amplifier of the
stall frequency; the stall itself is connection-specific (the paper owner
runs identical stream code on the same box and has never tripped).

## Fixed this session (commits `2c6703a`, and the observability commit after it)

- Paper fleet Telegram notifications (the audit's confirmed coverage gap:
  the entire paper fleet emitted zero positive notifications): the paper
  owner now drives the shared notification engine with a `Bybit paper`
  heading and a `🧪 PAPER · integration-only twin` label on every page;
  demo output is byte-identical; paper producers still scrub Telegram
  variables; provisioning seeds/preserves the paper Telegram credentials and
  still forbids venue credentials in the paper environment.
- `on_error`/`on_close`/connected logging on the raw public stream — every
  future disconnect, failed reconnect, and recovery is journal-visible and
  correlatable with the watchdog alerts.
- Default package log handler (`logging_setup.ensure_default_log_handler`)
  in both account owners — batch receipts and INFO-level evidence reach
  journald (both owners were journald-silent in normal operation).
- One-line delivery audit trail per sent Telegram page (context, page,
  chars, first line) — sent content was previously logged nowhere.

Takes effect at the next owner-dispatched rollout.

## Open findings, ranked (all independently verified unless noted)

1. **Demo-rule receipt expires 2026-07-29T21:57:44Z (~60 h)** — scheduled
   fail-closed break: running units keep running, but any unit
   (re)start after expiry fails closed, and the pending-reboot kernel
   updates make an untimed reboot a fleet-down scenario. The rollout path
   auto-refreshes rules only when the receipt is already expired
   (`classify_demo_rule_receipt_freshness` returns `expired` strictly after
   168 h). Watchdog warns from 24 h out. Cleanest play: dispatch the next
   rollout shortly after expiry (it refreshes receipts, installs the pending
   commits, and restarts the fleet in one pass), then reboot for the kernel
   updates.
2. **Host CPU/memory saturation** (load ~5–6 on 2 vCPU, swap in use) —
   upsize or shed load; the watchdog's 3-minute full-authorization
   revalidation (~13 s CPU per run) is the biggest sheddable cost. The
   follow-up pass added a hedge-run wall-time alert (>180 s) so cadence
   overrun pages instead of silently compounding.
3. **No external dead-man's switch** — `LIVENESS_HEARTBEAT_URL` is designed
   but unprovisioned; total box death silences the watchdog and its alert
   channel together. Needs an owner-provisioned heartbeat URL. The
   follow-up pass makes the (future) heartbeat also page on
   notification-channel death: the ping is now suppressed whenever a
   Telegram send fails.
4. **Single Telegram channel, no delivery-failure escalation** — partially
   FIXED in the follow-up pass: the watchdog now alerts when the demo
   owner's hourly digest stops committing (`account_digest_stale`), added a
   disk-space alert (WARNING >80 % / CRITICAL >90 %), and suppresses the
   external heartbeat on send failures; full independence still requires
   the owner-provisioned heartbeat URL.
5. **Reconnect-loop detection hole** — FIXED in the follow-up pass: the
   stream now keeps a cumulative last-accepted-frame timestamp that
   survives socket detach; the watchdog warns once per silent window and
   force-closes any half-dead attempt, and logs recovery when frames flow
   again.
6. **Owner process died once on a REST timeout** (Jul 26 03:05, ErrCode
   10000 killed the process; systemd restarted it in ~20 s) — FIXED in the
   follow-up pass: the periodic reconcile catches transport failures, logs,
   and retries next interval; the health chain's recency requirement still
   fails closed if refreshes keep failing. Startup reconciliation stays
   strict.
7. Lower severity: continuous-hedge oneshot consumes 72–95 s of its ~5-min
   cadence on the saturated host; hedge targets are below the venue floor at
   current scale (zero hedge fills ever — see the verification below;
   correction: the binding constraint for BTCUSDT is min_qty 0.001 BTC,
   ~65 USDT, not ~5 USDT min notional); journald at 762 MB and 5 stale
   copies of the secrets env file — FIXED at next rollout (deploy now caps
   journald at `SystemMaxUse=1G` and prunes all but the newest credential
   backup); `LONG_PAPER_SLEEVE` toggle missing — FIXED (watchdog-side
   toggle, defaulting to the demo `LONG_SLEEVE` so nothing regresses);
   legacy strategy-local ledgers misread as "no trading since deploy";
   cycles dataset `equity_usdt=0.0` sentinel rows — FIXED (both daemons now
   record null when owner health is unavailable; kill criteria were never
   affected — they read the account journal, not the cycles dataset).

## 2026-07-27 follow-up: BTC hedge and BTC gate verified legit-as-designed

Two independent probes, each adversarially re-verified against primary
sources (code, the immutable prior, the VPS journal, and public market
data):

- **Hedge sizing** — the 3.92 USDT BTC target against the 147.67 USDT short
  book (symbol `4USDT`, qty −14601 — the symbol name, not $4 of exposure)
  reproduces **bit-identically** from first principles:
  `target = clip(−beta_btc, 0, 2) · intensity · short_gross / 0.5` with
  prior betas `beta_btc = −0.02213`, `beta_eth = +0.000248` (ETH leg 0 via
  the long-only clip) and btcvol intensity 0.6. Equity cancels; only short
  gross matters. Zero fills ever is the designed outcome: the sub-step
  intent is deliberately preserved so the kernel emits an explicit
  `qty_step_mismatch` rejection (22 ever: 8 on Jul 25 against the DEXEUSDT
  short, 14 during the 4USDT window; deduplicated, no Telegram spam). The
  hedge first becomes executable at short gross ≈ 2.5–4.9 k USDT
  (intensity 0.6) — comfortably reachable under the 20 k gross cap for a
  normally-sized book. The tiny hedge is the 2026-07-24 measurement ("beta
  hedging relieves only 5 %") operating as designed, and the
  `hedged_V3_proposal` parity render uses the identical construct.
- **BTC trend gate** — the value (0.0883 on Jul 27) is the sum of the prior
  30 UTC-day BTC daily returns, current day excluded; every distinct value
  the tape has ever carried reproduces **bit-for-bit** from public Bybit
  daily klines. PIT-safe twice over (fetch window ends at the last closed
  hour; the accumulation loop cannot leak a day's own return into its own
  gate value). "Uptrend allows short-fade entries" is the measured design
  (ungated book ≈ dead: +1.29 bp/day vs +41.09 gated), not a sign error.
- **One actionable item (medium)**: the hedge model prior
  (`deploy/hedge_warmstart/bybit_warmstart.csv`, generated 2026-07-10 from
  the OLD 3-component ensemble ledgers) predates the `single_fund0`
  replacement; `docs/hedge_refresh_policy.md` schedules regeneration after
  each standard research refresh, which is now due. It requires the fresh
  component ledgers from the standard continuous equity refresh (not on
  disk locally), so it is queued as the next research-ops action rather
  than executed blind. Practical impact today is nil — any plausible beta
  stays far below the venue floor at the current book size.
- Deliberate non-change: no `below_venue_minimum` status was added to the
  hedge runner — the sub-minimum path already produces an explicit,
  deduplicated, journaled kernel rejection by design.

## Verified healthy

- The deployed revision behaves exactly as designed: clean change point
  15:19 Jul 26 (last 3-component row 14:59:10, first single-`p3` schema-v2
  row 15:19:20, zero old-shape rows after); funding admission live and
  counted (14/14 age-passing candidates admitted, 0 rejected, 0
  unknown-admits — no probe fail-open storm); one full round trip (4USDT
  short, TP/SL at exactly 12 %/35 %, take-profit fired, +18.15 gross);
  hedge and rmom timers healthy; equity continuous across the change point.
- The paper twin is alive, wired, isolated, and monitored: coherent
  66-event journal, 6/6 intents completed, three full round trips,
  credential isolation enforced in code + unit config + filesystem, and the
  watchdog actively covers paper units, health, capture, and both paper
  cycle datasets.
- Paper caveats to remember when reading its record (documented design, not
  bugs): no funding P&L accrual (integration-only scope), no hedge route
  (demo-only by design, `docs/operations.md`), fixed non-compounding
  capital base.
- Host basics: git/python/NTP/unit permissions/OOM/disk all clean.
