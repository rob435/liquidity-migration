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
profile is a ratio of it ([`equity_anchored_envelope.py`](../liquidity_migration/equity_anchored_envelope.py)).

Equity down rescales the caps on the next observation. Equity up waits for a
move larger than `expand_dead_band_fraction` (5%), so ordinary wander cannot
re-scale the book every cycle. Missing, non-finite, or stale equity holds the
current reference; below `floor_usdt` it clamps rather than collapsing. In this
mode the producer's own equity clamp is disabled — the owner's caps bind.

`account_risk.sleeve_limits` partitions the account gross and margin caps into
per-sleeve shares that must sum inside those caps. The kernel holds each sleeve
to its share on every exposure-increasing batch and refuses a sleeve the
partition does not name ([`account_kernel.py:2367`](../liquidity_migration/account_kernel.py)).
Risk-reducing batches bypass every cap, so exits are always possible.

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
| Absolute pre-trade caps — component gross, account gross, initial margin, available margin, leverage — rejected atomically inside the journal transaction | [`account_kernel.py`](../liquidity_migration/account_kernel.py) |
| Per-sleeve partition | [`account_kernel.py`](../liquidity_migration/account_kernel.py), [`operational_profile.py`](../liquidity_migration/operational_profile.py) |
| Caps rescale with observed equity | [`equity_anchored_envelope.py`](../liquidity_migration/equity_anchored_envelope.py) |
| Daily loss halt → `run_safety_flat_once` | [`account_loss_guard.py`](../liquidity_migration/account_loss_guard.py) |
| Venue-native stop armed in the same `place_order` call, read back after create | [`venue_protection.py`](../liquidity_migration/venue_protection.py), [`bybit_execution_adapter.py`](../liquidity_migration/bybit_execution_adapter.py) |
| One owner per account; journal ↔ venue reconciliation | [`account_owner_lease.py`](../liquidity_migration/account_owner_lease.py), [`account_reconcile.py`](../liquidity_migration/account_reconcile.py) |
| Mainnet client refuses to construct while `REAL_MONEY` is unset | [`bybit.py:227`](../liquidity_migration/bybit.py) |
| Producers get no credentials and no arming switch in any realm; order authority is the account owner's alone | the mainnet units `UnsetEnvironment` both |

## Arming (owner-executed)

Run `scripts/ops.sh real-money preflight` at any point to see which of these is
still outstanding. `LOCAL=1` runs it against this checkout instead of the VPS.

1. **Confirm the account is flat.** No manual position, no open order.
2. **Create the API key** on the funded account: contract trading only,
   **withdrawal disabled**, IP-allowlisted to the VPS.
3. **Fill in the credential file.** Copy
   [`deploy/bybit-mainnet.env.template`](../deploy/bybit-mainnet.env.template) to
   `/etc/liquidity-migration/bybit-mainnet.env`, root-owned `0600`. Paste into
   `BYBIT_REAL_API_KEY` / `BYBIT_REAL_API_SECRET` — deliberately different
   variables from the demo pair — set the dials, and set `REAL_MONEY=true`, the
   single act that means "trade my money".
4. **Copy the route file.**
   [`deploy/account-execution-mainnet.env.template`](../deploy/account-execution-mainnet.env.template)
   to `/etc/liquidity-migration/account-execution-mainnet.env`, root-owned `0600`.
   Its roots are disjoint from demo and paper.
5. **Create the state roots**, root-owned:
   `mkdir -p /var/lib/liquidity-migration/{account,inbox,capture}-mainnet`.
6. **Render the profile** (`D=/etc/liquidity-migration/account-execution-mainnet`):
   ```bash
   scripts/ops.sh real-money render-profile --execute --output $D/risk-policy.json
   ```
7. **Freeze the inputs.** Universe first, then rules against it:
   ```bash
   scripts/freeze_account_candidate_universe.py --output $D/candidate-universe.json
   scripts/freeze_venue_instrument_rules.py --realm mainnet \
     --symbols-file $D/candidate-universe.json --output $D/venue-rules.json
   ```
   Rules come from the read-only `get_instruments_info` endpoint. Do not run the
   demo order probe: it places live PostOnly orders and refuses any realm but
   demo by name ([`demo_rule_probe.py`](../liquidity_migration/demo_rule_probe.py)).
8. **Enable the producers.** Set `CARRY_MAINNET_SLEEVE` and/or
   `LONG_MAINNET_SLEEVE` to `on` in [`deploy/sleeves.env`](../deploy/sleeves.env)
   and commit — repo-off is a ceiling a host override cannot lift. Then
   `scripts/ops.sh deploy --execute install`, which puts every unit file on disk
   and leaves all of them disabled.
9. **Start the mainnet units by hand**, at Tier 1 size. No deploy mode does it.

Changing any dial afterwards means re-rendering the profile and reinstalling.

## What preflight checks

[`liquidity_migration/real_money_arming.py`](../liquidity_migration/real_money_arming.py)
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

## What is missing before this could run live

- **No mainnet activation path.** `deploy_vps_live.sh activate` starts demo and
  paper units only; `verify` asserts all three mainnet units are *off* and fails
  if any is active. Starting them is manual `systemctl`, and the next `verify`
  then fails on purpose.
- **Nothing creates the mainnet state roots.** Preflight reports them missing and
  prints the `mkdir`; the owner runs it.
- **No mainnet watchdog.** `liquidity-migration-demo-liveness.timer` scopes itself
  to `demo`/`demo-paper`. The owner's Telegram is the only alive signal, and an
  absent process cannot report its own absence. Watch it during Tier 1.
- **The candidate universe is not mainnet-sourced.**
  `freeze_account_candidate_universe.py` reads the public *demo* endpoint and
  stamps the artifact `demo`; producers load it with that default. Same linear
  perpetual set on both realms, but not evidence of the mainnet listing.
- **Untried against a real venue.** The post-create stop assertion, the
  reconciler, and the wedged-command path have never run against a mainnet key.
  The code is tested; the venue behaviour it asserts is not.
- **Known rough edges.** Reconcile staleness floors are 30 s for funding and
  15 s for positions (`account_reconcile.py`) and may still trip on mainnet
  latency; release-to-pending retries without a ceiling; `account_gross` comes
  from target quantities, not venue position value; margin tiers unmodelled.

## Record

Bybit demo prices are real; its fills are not, and paper fills are modelled
locally. CARRY (`lane2_carry_hold_v3`) runs on demo; LONG's forward record is
demo-only. `carry_hold`'s benchmark Sharpe is **1.21 (t 2.31)** and does not
beat the CONTINUOUS benchmark; the superseded 2.57 / t 4.87 figures were
double-counted funding ([`carry_hold.md`](carry_hold.md),
[`../AGENTS.md`](../AGENTS.md), [`strategy_program.md`](strategy_program.md)).

## Ramp

| Tier | Capital | Gate to the next tier |
| --- | --- | --- |
| 1 | venue minimums on 3 names | 5 days: no unexplained journal/venue mismatches, no protection-stale events, no resize churn, realised cost within 1.5× the 15.56bp model |
| 2 | 10% of ceiling | 10 more days clean, plus one funding-regime turn survived |
| 3 | 50% | 20 more days clean, plus one ≥5% drawdown handled unattended |
| 4 | 100% | 30 more days clean |

Tier 1 buys real fills, not returns. Any breach drops to 0, not one tier down.
