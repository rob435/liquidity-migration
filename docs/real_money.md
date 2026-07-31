# Real Money

CARRY and LONG on one funded Bybit account, each holding a private share of an
envelope scaled to observed wallet equity.

`REAL_MONEY` is unset, no mainnet credential exists, and no mainnet unit has
started. Arming is the owner's own act, step by step under *Arming* below.
Plain-language version: [`docs/plain_english_guide.md`](plain_english_guide.md).

## The envelope

No money amount binds anything. `capital_reference_usdt` in
[`configs/operational.mainnet.json`](../configs/operational.mainnet.json) is
`2500.0`, and it is only a scale: `capital_reference.mode = "account_equity"`
makes the runtime reference track observed wallet equity, and every cap in the
profile is a ratio of it ([`equity_anchored_envelope.py`](../liquidity_migration/policy/equity_anchored_envelope.py)).

Equity down rescales the caps on the next observation. Equity up waits for a
move larger than `expand_dead_band_fraction` (5%), so ordinary wander cannot
re-scale the book every cycle. Missing, non-finite, or stale equity holds the
current reference; below `floor_usdt` it clamps rather than collapsing. In this
mode the producer's own equity clamp is disabled — the owner's caps bind.

`account_risk.sleeve_limits` partitions the account gross and margin caps into
per-sleeve shares that must sum inside those caps. The kernel holds each sleeve
to its share on every exposure-increasing batch and refuses a sleeve the
partition does not name ([`account_kernel.py:2367`](../liquidity_migration/account/account_kernel.py)).
Risk-reducing batches bypass every cap, so exits are always possible.

A dedicated subaccount would put the ceiling at the venue instead of in
software. Declined, so these caps are what hold size.

## Dials

One file: [`deploy/bybit-mainnet.env.template`](../deploy/bybit-mainnet.env.template)
→ `/etc/liquidity-migration/bybit-mainnet.env`, root-owned `0600`. Every dial is
optional (omitting one takes the committed default) and every dial is a ratio
except the floor.

| Dial | Default | Meaning |
| --- | --- | --- |
| `RM_EQUITY_FRACTION` | 1.0 | Fraction of the wallet the envelope scales to. Cannot exceed 1. |
| `RM_EQUITY_FLOOR_USDT` / `RM_EXPAND_DEAD_BAND_FRACTION` | 100.0 / 0.05 | Reference floor (the one dial that is an amount) and the expansion-only dead band, in `[0, 1)`. |
| `RM_MAX_LEVERAGE` / `RM_ENTRY_LEVERAGE` | 2.0 / 2.0 | Hard ceiling — the renderer refuses above 2.0 — and what producers request. Entry ≤ max. |
| `RM_ACCOUNT_GROSS_MULTIPLE` | 2.0 | Gross ceiling ×reference. Bounded by leverage and by `entry_leverage × initial_margin_fraction`. |
| `RM_SYMBOL_NOTIONAL_FRACTION` | 0.5 | Largest single-symbol position. |
| `RM_INITIAL_MARGIN_FRACTION` | 1.0 | Margin ceiling. Cannot exceed 1. |
| `RM_DAILY_LOSS_FRACTION` | 0.1 | Daily realised-loss halt. Trips a flatten. |
| `RM_CARRY_GROSS_SHARE` / `RM_LONG_GROSS_SHARE` | 0.55 / 0.40 | Per-sleeve shares of the gross and margin caps. With the 0.01 retired-CONTINUOUS token share they must sum ≤ 1. |
| `RM_CARRY_STOP_LOSS_FRACTION` | 0.35 | Venue-native stop distance, armed with the entry. |
| `RM_CARRY_NOTIONAL_MULTIPLIER` / `RM_LONG_NOTIONAL_MULTIPLIER` | 1.0 / 0.4 | Per-sleeve sizing. |

Plus `RM_{CARRY,LONG}_MAX_NEW_ENTRIES_PER_CYCLE` (10 / 5, positive integers) and
`RM_LONG_MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY` (0.5).
`scripts/ops.sh real-money render-profile` turns all of them into the profile the
kernel enforces; a dial set that cannot produce a loadable profile is refused
there, naming the dial to move, instead of at start-up over a funded account.

## Capital controls in force

| Control | Where |
| --- | --- |
| Absolute pre-trade caps — component gross, account gross, initial margin, available margin, leverage — rejected atomically inside the journal transaction | [`account_kernel.py`](../liquidity_migration/account/account_kernel.py) |
| Per-sleeve partition | [`account_kernel.py`](../liquidity_migration/account/account_kernel.py), [`operational_profile.py`](../liquidity_migration/policy/operational_profile.py) |
| Caps rescale with observed equity | [`equity_anchored_envelope.py`](../liquidity_migration/policy/equity_anchored_envelope.py) |
| Daily loss halt → `run_safety_flat_once` | [`account_loss_guard.py`](../liquidity_migration/policy/account_loss_guard.py) |
| Venue-native stop armed in the same `place_order` call, read back after create | [`venue_protection.py`](../liquidity_migration/venue/venue_protection.py), [`bybit_execution_adapter.py`](../liquidity_migration/venue/bybit_execution_adapter.py) |
| One owner per account; journal ↔ venue reconciliation | [`account_owner_lease.py`](../liquidity_migration/account/account_owner_lease.py), [`account_reconcile.py`](../liquidity_migration/venue/account_reconcile.py) |
| Independent watchdog: owner, producers, strategy inputs and venue snapshot every 3 min, paging Telegram; no credential, no ordering edge to the owner it watches | `liquidity-migration-mainnet-liveness.timer` → [`check_fleet_liveness.py --account-scope mainnet`](../scripts/runtime/check_fleet_liveness.py) |
| Mainnet client refuses to construct while `REAL_MONEY` is unset | [`bybit.py:204-209`](../liquidity_migration/venue/bybit.py) (private WebSocket: `:875-878`) |
| Producers get no credentials and no arming switch in any realm; order authority is the account owner's alone | the mainnet units `UnsetEnvironment` both |

## Arming (owner-executed)

Run `scripts/ops.sh real-money preflight` at any point to see which of these is
still outstanding. `LOCAL=1` runs it against this checkout instead of the VPS.

1. **Confirm the account is flat by hand.** No manual position, no open order. The owner's
   startup check for this (`require_bybit_order_ownership`,
   [`account_service_bybit.py`](../liquidity_migration/venue/account_service_bybit.py)) now runs in
   both realms, but it and the reconciler see USDT-settled linear only, so anything else on the
   UID stays invisible to both — see *What is still unproven*.
2. **Create the API key** on the funded account: contract trading only,
   **withdrawal disabled**, IP-allowlisted to the VPS.
3. **Fill in the credential file.** Copy
   [`deploy/bybit-mainnet.env.template`](../deploy/bybit-mainnet.env.template) to
   `/etc/liquidity-migration/bybit-mainnet.env`, root-owned `0600`, edited on the
   VPS by hand — the key and secret never enter an agent session. Paste into
   `BYBIT_REAL_API_KEY` / `BYBIT_REAL_API_SECRET` — deliberately different
   variables from the demo pair — set the dials, and set `REAL_MONEY=true`, the
   single act that means "trade my money".
4. **Copy the route file.**
   [`deploy/account-execution-mainnet.env.template`](../deploy/account-execution-mainnet.env.template)
   to `/etc/liquidity-migration/account-execution-mainnet.env`, root-owned `0600`.
   Its roots are disjoint from demo and paper.
5. **Create the state roots** from the route file just copied:
   ```bash
   scripts/ops.sh real-money create-state-roots            # lists what it would create
   scripts/ops.sh real-money create-state-roots --execute  # creates them, mode 0700
   ```
   It refuses a relative root, a root that is not a directory, and any root at or
   inside a directory the demo or paper owner env declares.
6. **Render the profile** (`D=/etc/liquidity-migration/account-execution-mainnet`):
   ```bash
   scripts/ops.sh real-money render-profile --execute --output $D/risk-policy.json
   ```
7. **Freeze the inputs.** Universe first, then rules against it:
   ```bash
   scripts/maintain/freeze_account_candidate_universe.py --realm mainnet \
     --output $D/candidate-universe.json
   scripts/maintain/freeze_venue_instrument_rules.py --realm mainnet \
     --symbols-file $D/candidate-universe.json --output $D/venue-rules.json
   ```
   Rules come from the read-only `get_instruments_info` endpoint. Do not run the
   demo order probe: it places live PostOnly orders and refuses any realm but
   demo by name ([`demo_rule_probe.py`](../liquidity_migration/venue/demo_rule_probe.py)).
8. **Enable the producers.** Set `CARRY_MAINNET_SLEEVE` and/or
   `LONG_MAINNET_SLEEVE` to `on` in [`deploy/sleeves.env`](../deploy/sleeves.env)
   and commit — repo-off is a ceiling a host override cannot lift. Then
   `scripts/ops.sh deploy --execute install`, which puts every unit file on disk
   and leaves all of them disabled.
9. **Start the fleet**, at Tier 1 size: `scripts/ops.sh deploy --execute activate-mainnet`.
   It re-runs preflight and refuses while anything above is outstanding. To stop
   it again: `scripts/ops.sh deploy --execute stop-mainnet` — publication only,
   exposure unchanged.

Steps 8 and 9 are deploy modes, so they need `EXPECTED_COMMIT` set to the full
40-character commit like every other one ([`operations.md`](operations.md)).
Changing any dial afterwards means re-rendering the profile and reinstalling.

## What preflight checks

[`liquidity_migration/policy/real_money_arming.py`](../liquidity_migration/policy/real_money_arming.py)
reads only, reports a credential by name and never by value, takes `--json`, and
exits 1 while anything is outstanding.

| Check | Passes when |
| --- | --- |
| Both env files | Exist, root- or caller-owned `0600`, parse as strict `KEY=value` |
| Credentials and switch | `BYBIT_REAL_API_KEY` / `_SECRET` non-empty, both demo variables absent, `REAL_MONEY` a recognised true value — an unrecognised value fails rather than being guessed |
| Notifications | `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` both set — the unit sets `TELEGRAM_ENABLED=1` and refuses to start half-notified |
| Dials | Parse, render, and load; reports the resulting leverage, gross multiple, and partition shares |
| Owner route | All 12 route keys declared, `ACCOUNT_VENUE_REALM=mainnet`, `CANDIDATE_UNIVERSE_FILE` == `ACCOUNT_SYMBOLS_FILE` |
| State roots and artifacts | Three mainnet directories exist; universe, rules, and profile files present |
| Profile matches dials | The installed profile is byte-identical to the render of the current dials — the likeliest arming mistake is editing a dial and forgetting to re-render |
| Sleeve toggles | At least one mainnet sleeve `on` in `/etc/liquidity-migration/sleeves.resolved.env` |

## What is still unproven

The owner's four Bybit REST components — wallet snapshot, start-up
order-ownership check, position reconciler, funding reconciler — each refused a
non-demo client outright until 2026-07-31 and now construct and run under either
named realm. Every step above exists and is tested, none of it has run against a
funded account, and the first bullet is a blocker rather than a caveat: two
demo-only client fences remain, the first of them ahead of all four, so a
mainnet owner still does not start.

- **Two demo-only client fences still block mainnet startup.**
  `BybitNativeProtectionManager` refuses a non-demo client
  ([`venue_protection.py:151`](../liquidity_migration/venue/venue_protection.py)) and is built at
  [`account_service_runner.py:716`](../liquidity_migration/runtime/account_service_runner.py), before
  every other start-up read; `BybitDemoExecutionAdapter` refuses one too
  ([`bybit_execution_adapter.py:91`](../liquidity_migration/venue/bybit_execution_adapter.py)) and is
  built after them. Both are the same arbitrary realm fence the wallet snapshot, order-ownership
  and reconciler gates carried until they were made realm-aware, but the protection manager also
  supplies the ownership check's native verifier, so un-fencing it changes what a mainnet owner
  treats as its own stop. Owner decision.
- **The ownership and position reads are realm-aware but narrow, and fail open.** Both run in
  both realms now, and both ask Bybit for `category=linear, settleCoin=USDT` only. A USDC-settled,
  inverse, spot or options order *or position* on the same UID is therefore invisible: startup
  passes, the reconciler reports `healthy` with that exposure omitted, and nothing detects it —
  while `totalEquity` (below) does count it, so the caps and the daily loss halt size against
  exposure the owner cannot see or close. Flatten and keep the UID to USDT-linear by hand.
  Bybit's own liquidation and ADL orders carry no kernel
  `orderLinkId` and no stop provenance, so they classify unowned — that makes the account
  unhealthy and, through `require_recent_symbols_consistent`, blocks the owner's own reduce-only
  close on the symbol being liquidated. Hedge mode is unsupported and shows up as
  `dual_side_position_not_supported`, not as a named precondition failure.
- **Preflight never reads the wallet.** `real_money_arming.preflight` checks files, dials and
  routes; the first authenticated wallet read happens at owner bootstrap. So a UNIFIED row that
  blanks every account-wide equity field (the owner refuses to start and names the blank
  fields), a non-UTA account, or a hedge-mode account surfaces as a `Restart=always` loop rather
  than a preflight line. Which account or margin modes blank that row is unverified against a
  funded account, so no message here names one. The snapshot also needs
  `totalAvailableBalance`, or `totalMarginBalance` and `totalInitialMargin`, so a row blanking
  only some of them still fails on the opaque per-field message instead.
- **Wallet equity counts non-USDT collateral.** `totalEquity` and `totalAvailableBalance` are
  account-wide USD aggregates over every asset, so on a mixed-collateral account the envelope
  scales the whole profile off assets the strategy neither trades nor can post as USDT margin —
  and a collateral drawdown alone rescales the caps and can trip the daily loss halt. The
  kernel's available-margin rejection bounds the damage to blocked batches. `RM_EQUITY_FRACTION`
  is the dial for it; 1.0 is only right on a USDT-only account.
- **Three funding shapes stop the owner permanently, outside the degrade path.** A
  `category=linear` settlement row in any currency but USDT raises
  ([`account_reconcile.py:728`](../liquidity_migration/venue/account_reconcile.py)); the epoch replay
  paginates at 2,500 rows per 7-day chunk — about 357 settlements/day — above which every
  restart fails; and off demo a settlement row carrying nonzero `cashFlow`, which would be
  double-counted against reconstructed fill P&L, is refused rather than booked (`:773`). The
  first two apply in both realms; only the third is realm-gated. All three raise out of
  `reconcile_once`, and the runner calls that at
  [`account_service_runner.py:794`](../liquidity_migration/runtime/account_service_runner.py) and
  `:810` outside `degrade_or_raise` — so each is a `Restart=always` crash loop with the
  book unmanaged, not the exit-only degradation the wrapped start-up checks fall back to. Venue
  rows are immutable and every restart replays the epoch from the first journal event, so one
  such row is permanent until it is dealt with by hand.
- **Demo still books a `cashFlow`-bearing settlement, and would double-count it.** The refusal
  above is mainnet-only because demo behaviour was held fixed across the realm change. Demo
  has only ever returned funding-only rows, so the double count is latent rather than
  observed, and a test now pins the demo booking. Whether demo should refuse too — trading an
  accounting defect for a crash loop — is an open owner decision, and it is the same trade the
  mainnet refusal already makes.
- **Nothing refreshes the mainnet candidate universe.** `--realm mainnet` freezes
  it from `api.bybit.com` and the mainnet producers load it under that realm, but
  the automatic refreeze in `deploy_vps_live.sh` is demo-only. Re-freeze by hand
  to admit a new listing; a retirement is handled in-cycle.
- **The watchdog's mainnet scope is unexercised.**
  `liquidity-migration-mainnet-liveness.timer` runs `check_fleet_liveness.py
  --account-scope mainnet` every three minutes and pages over the mainnet
  credential file's Telegram pair, holding no trading authority. Its thresholds
  were chosen, not measured against a funded account. Watch it during Tier 1
  rather than relying on it.
- **`stop-mainnet` stops publication, not exposure.** It leaves positions and the
  sleeve toggles alone, so `verify` fails afterwards and the next `activate` or
  `rollout` restarts the fleet. Flatten through the account owner and turn the
  toggles off to make a stop stick.
- **Untried against a real venue.** The post-create stop assertion, the
  reconciler, and the wedged-command path have never run against a mainnet key.
  The code is tested; the venue behaviour it asserts is not.
- **Known rough edges.** Reconcile staleness floors are 30 s for funding and
  15 s for positions (`account_reconcile.py`) and the owner's health-age bound is
  `reconcile_seconds × 2` (4 s as shipped); all three may trip on mainnet
  latency. A stale protection sync raises exactly like an absent stop, so the
  alarm cannot tell unverified from unprotected. Release-to-pending retries
  without a ceiling; `account_gross` comes from target quantities, not venue
  position value; margin tiers unmodelled.

## Record

Bybit demo prices are real; its fills are not, and paper fills are modelled
locally. CARRY (`lane2_carry_hold_v3`) runs on demo; LONG's forward record is
demo-only. `carry_hold`'s benchmark Sharpe is **1.21 (t 2.31)** and does not
beat the CONTINUOUS benchmark; the superseded 2.57 / t 4.87 figures were
double-counted funding ([`carry_hold.md`](carry_hold.md),
[`../AGENTS.md`](../AGENTS.md), [`strategy_program.md`](strategy_program.md)).

### Measured on the demo account, 2026-07-30

Read from the venue, not from a backtest. 266 closes over 2026-06-05 → 07-30
total **+$3,200.21**, but +$2,902.11 of that booked on 07-30 alone: 133 of the
day's closes were `carry resize: depth rescale`, the mark-to-market sizing
defect fixed in `b13cbfac3` moving unrealised gain into the realised column.
The price-return record is **+$298 over eight weeks on a $250,000 reference**.

Funding is the thesis and it is real, checked against public rate history:
lifetime settlement **+$3,048.83**, of which −$148.88 predates CARRY v3, when
the account was paying. Rates that high come from squeezed small-cap alts, so
the payoff is negative-skew and regime-dependent — `LAUSDT` paid 6.355%/day
(worst 4h −1.96%) while its price fell 7.6% since entry.

Fees: $85.68 lifetime, $70.78 of it in the 1.5 days after CARRY v3, mostly the
same defect. Verify the post-fix run rate before any ramp.

Exit capacity on a calm book, all filling inside 200 levels:

| Symbol | Held | Spread | Full-position exit |
| --- | --- | --- | --- |
| `VANRYUSDT` | $9,409 | 2.5bp | 12.9bp |
| `LAUSDT` | $25,677 | 1.9bp | 27.7bp |
| `ESPUSDT` | $25,537 | 1.4bp | 23.4bp |

The 15.56bp round-trip model is about right, mildly optimistic for `LAUSDT`.
It is not a stress estimate: depth into a 35% gap-down is a fraction of this and
slippage scales superlinearly with size.

## Ramp

| Tier | Capital | Gate to the next tier |
| --- | --- | --- |
| 1 | venue minimums on 3 names | 5 days: no unexplained journal/venue mismatches, no protection-stale events, no resize churn, realised cost within 1.5× the 15.56bp model |
| 2 | 10% of ceiling | 10 more days clean, plus one funding-regime turn survived |
| 3 | 50% | 20 more days clean, plus one ≥5% drawdown handled unattended |
| 4 | 100% | 30 more days clean |

Tier 1 buys real fills, not returns. Any breach drops to 0, not one tier down.
