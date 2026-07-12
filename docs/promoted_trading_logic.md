# Active Trading Logic

Last manually reconciled with code and deploy files: 2026-07-10. Verify the
current sources below before acting.

This file describes the active profile lifecycle. It is not a research diary.
Historical receipts are in git history and summarized in
`docs/preregistration/INDEX.md`.

The module name `promoted.py` is a compatibility/runtime registry name. It does
not imply that a profile passed a research gate, is paper-ready, or is authorized
for mainnet. Evidence is judged under `docs/governance.md`; runtime state is
descriptive.

## Source Order

When files disagree, fix them in one change. Read in this order:

1. The read-only VPS verifier plus
   `/etc/liquidity-migration/sleeves.resolved.env` and systemd state for what is
   effectively running.
2. `deploy/sleeves.env` for the repository deployment ceiling/default; host
   overrides may narrow an `on` sleeve to `off`.
3. `liquidity_migration/promoted.py` for registry objects.
4. `liquidity_migration/continuous_demo.py` and
   `liquidity_migration/continuous_forward_replay.py` for continuous runtime and
   replay config.
5. `liquidity_migration/long_native_event_demo.py` for long v11a runtime config.
6. `deploy/systemd/*.service` for repository service env; compare it with the
   effective VPS unit state.

## Running Surface

The repository ceiling in `deploy/sleeves.env` currently requests:

| Sleeve | Toggle | Runtime |
| --- | --- | --- |
| Long demo + paper | `LONG_SLEEVE=on` | bybit long event engines |
| Continuous demo | `CONTINUOUS_SLEEVE=on` | bybit continuous event engine |
| Continuous paper | `CONTINUOUS_PAPER_SLEEVE=on` | bybit continuous dry-run engine |

Paper services submit no orders. They write comparison ledgers.

## Continuous v2 Fade Book

Registry object: `promoted.continuous_profile()` returns a deep copy of
`FROZEN_FORWARD_CONFIG`.

Live daemon profile: `STRATEGY_PROFILE=continuous_ensemble_v2`.

Data roots:

- Demo: `data/bybit-continuous-demo-event`.
- Paper: `data/bybit-continuous-paper-event`.

### Signal

- Short fade entries use the top composite decile after gates and liquidity
  filters.
- `BTC_TREND_GATE=uptrend`.
- `rmom_quantile=0.25`.
- `feature_set=("max_ret168",)`.
- `liq_turnover_min=500000`.
- Entry timing uses confirmed bars with `entry_confirm_delay_hours=1`.
- Max active shorts: 25.
- Max new entries per cycle: 5.
- Adverse-exit pause: 8 exits inside 1440 minutes.

### Components

| Tag | Trigger | Age floor | TP | Weight |
| --- | --- | ---: | ---: | ---: |
| `p3` | `turn3_pop3` | 240d | 12% | 0.3333333333333333 |
| `p4p3` | `turn4_pop3` | 240d | 12% | 0.2222222222222222 |
| `p4p5` | `turn4_pop5` | 240d | 12% | 0.4444444444444444 |

### Sizing

Live order notional:

```text
equity
* wallet_balance_fraction
* (PER_POSITION_NOTIONAL_PCT_EQUITY / 100)
* NOTIONAL_MULTIPLIER
* rebalance_scale
* component_weight
* vol_weight_multiplier
* btc_risk_stack_mult
```

Registered/base-profile knobs:

- `ENTRY_LEVERAGE=2`.
- `PER_POSITION_NOTIONAL_PCT_EQUITY=2`.
- `NOTIONAL_MULTIPLIER=1`.
- `SIZING_MODE=inverse_vol`.
- `TARGET_VOL_PER_NAME=0.01`.
- `VOL_WEIGHT_CLAMP=2`.

The Bybit demo and matching paper units currently override
`ENTRY_LEVERAGE=10` and `NOTIONAL_MULTIPLIER=10` for an explicitly labelled
forward execution/lifecycle stress epoch. Exchange leverage changes margin;
the notional multiplier is what makes order quantities 10x the registered
base. This operational scale change does not alter the registered 1x research
object and its P&L cannot be presented as 1x validation.

`vol_weight_multiplier = target_vol_per_name / rv_168h`, clamped to
`[0.5, 2.0]`; missing or invalid `rv_168h` uses `1.0`.

`CTRL_BTC_RISK_70_90_35` is the current BTC-risk sizing overlay. After 50 prior
same-entry decisions, entries sharing a `(symbol, signal_ts)` use
`btc_risk_stack_mult=0.35` when the causal V0 BTC-risk score is in
`[0.70, 0.90)`. Otherwise the multiplier is `1.0`.

### Continuous Entry Safety

Submit-mode new entries pass a cycle-level risk-health gate before candidate
selection. It blocks new entries when private account snapshots have errors, when
a genuine private execution WS stream has emitted and then gone stale beyond
`ENTRY_PRIVATE_WS_STALE_SECONDS` / `--entry-private-ws-stale-seconds` (default
300 seconds), or when an open continuous ledger symbol is missing from the venue
position snapshot. It also blocks a live exchange-only position when the
continuous order ledger has a recent non-reduce-only entry attempt for that
symbol but no open continuous trade row. For profiles that expect venue stops
(`STOP_LOSS_PCT > 0`), it blocks new submitted entries while an open non-hedge
continuous position has no venue `stopLoss` protection in the private position
snapshot. The active v2 object is stopless (`STOP_LOSS_PCT=0`), so missing stops
remain telemetry rather than an entry-blocking reason for that profile. The gate
also records compact live lifecycle-state counts (`PROTECTED`,
`PROTECTION_PENDING`, `EXIT_ORDER_SUBMITTED`, `ORPHAN`, etc.) for open
continuous trades. Before submitted trade rows are flushed, the lifecycle
guard enforces an explicit trade-row transition table: terminal rows cannot be
reopened, close rows need prior ledger state, protected rows cannot silently
regress to `PROTECTION_PENDING`, and in-flight exit markers cannot be dropped.
Rejected rows are written to `continuous_risk_events.jsonl` and page as
lifecycle-transition violations. Healthy submitted cycles also persist
`PROTECTED` promotions from the private position snapshot onto full copied trade
rows for stop-required profiles; missing stops never demote ledger state.
Submitted live, paper and historical execution now use the shared canonical
journal and reducer described in `docs/canonical_execution_journal.md`. The old
continuous-only lifecycle JSONL is read solely as a compatibility fallback for
archived pre-migration roots; it is no longer written or authoritative.
Dry-run and paper evidence cycles keep running and record the same fields
without suppressing candidates. Blocked submit cycles append
`continuous_risk_events.jsonl` with `event=entry_risk_health_blocked`.

`ws_risk` appends stop/take-profit repair attempts to
`reports/event-risk-ws/stop_audit_events.jsonl` after sleeve tagging. These rows
are audit evidence for target/current protection and submit outcome; they do not
change routing.

Submit-mode portfolio heat is recorded on cycle rows. The cap itself is an
explicit live overlay, default OFF (`ENTRY_PORTFOLIO_HEAT_CAP_FRAC=0`), because
it is not part of the active v2 backtest selection object. If enabled, it caps
entries before candidate selection using current non-hedge open notional plus
conservative per-entry heat under `ENTRY_PORTFOLIO_HEAT_SHOCK_FRAC`.

Submit-mode account drawdown is also recorded on cycle rows. The kill-switch is
an explicit live overlay, default OFF
(`ENTRY_ACCOUNT_DRAWDOWN_KILL_SWITCH_FRAC=0`), for the same demo/paper/backtest
parity reason. If enabled, current wallet equity more than the configured
fraction below the prior healthy cycle high-water mark blocks new entries.
Wallet/private snapshot errors block separately and are not treated as drawdown
evidence from fallback equity.

Those parity choices describe a demo/paper research object only. They are not a
mainnet precedent: capital-preservation limits must be chosen from explicit
ruin/exposure constraints and need not improve an alpha metric or preserve
backtest parity (`docs/governance.md`).

### Continuous Exit Logic

Active exits:

- Component venue take-profit at 12%.
- `max_hold` force cover after 24 hours.
- `STOP_LOSS_PCT=0`; no venue/server disaster stop.

The stopless state is accepted only within the current demo/paper authorization.
It is not evidence that stopless mainnet risk is acceptable.

Disabled daemon exits that must not be silently reintroduced:

- `left_decile`.
- `stop_approach`.
- `failed_fade`.
- `breakeven`.
- Re-entry cooldown.

### Rebalance, Hedge, Add-ons

The deployed target has daily rebalance disabled, so `rebalance_scale=1.0`.
Historical replay context used `w90/tv0.045/max4/ddh=-0.04`; do not cite that
as the current deployed target without checking deploy state.

The frozen object includes:

- BTC+ETH 2-factor hedge.
- Beta window 90d, min observations 60.
- Hedge cap 2.0, hedge cost 5 bps.
- BTC-vol regime overlay with `lam=0.5`, `vol_window=30`, `pct_window=250`.

Sniper is disabled in demo and paper with `CONTINUOUS_SNIPER=0`. The former
quarter-size PostOnly sell at +8% was rolled back on 2026-07-10 after it added
loss into the forward 1000TAGUSDT squeeze without paper/backtest parity. It may
only return after a new preregistered two-venue replay and forward-paper plan.

Dynamic exit remains no-order paper shadow.

### Reconstruction Boundary

The official local continuous replay target is `FROZEN_FORWARD_CONFIG`: three
components, inverse-vol component sizing, 24h hold, 12% component TP, no stop,
BTC+ETH hedge, and BTC-vol regime.

The daemon adds live execution behavior, paper/demo state, optional flag-off
sniper plumbing, and the BTC-risk sizing overlay. A frozen component-ledger backtest is therefore not a
literal daemon replay unless it explicitly implements those state machines.

## Long-Native v11a Sleeve

Registry object: `promoted.long_profile()` returns
`_v11a_long_native_config()`.

Live daemon profile: `STRATEGY_PROFILE=LongV11aDivWeekendVol`.

Data roots:

- Demo: `data/bybit-long-demo-event`.
- Paper: `data/bybit-long-paper-event`.

### Signal And Entry

- Universe size 50 by trailing 90-day turnover.
- Minimum listing history 30 days.
- FC-only pattern gate: `enable_fomo_chase=True`.
- `fc_min_day_return=0.15`.
- `fc_top_volume_rank_max=10`.
- `fc_min_close_location=0.7`.
- BTC and ETH regime gates required.
- Multi-day close-location gate 0.6.
- `fc_max_atr_pct=0.12`.
- Sigma trigger enabled.
- `fc_sigma_mult=2.5`.
- 3-day and 7-day FC triggers enabled.

The live cycle uses fully closed daily bars and drops signals older than 24
hours. Entry uses v11a sniper retrace:

- `fc_sniper_retrace_pct=0.01`.
- `fc_sniper_deadline_hours=6`.
- Enter on 1% retrace below the signal close, or deadline fall-through.
- `fc_sniper_skip_on_no_retrace=False`.

### Sizing And Risk

Current systemd env:

- `NOTIONAL_MULTIPLIER=1`.
- `ENTRY_LEVERAGE=10`.
- `MAX_PROJECTED_INITIAL_MARGIN_PCT_EQUITY=0.5`.
- `MAX_ORDER_NOTIONAL_PCT_EQUITY=0`.
- `MAX_NEW_ENTRIES_PER_CYCLE=5`.
- `UNIVERSE_SIZE=50`.
- `LOOKBACK_DAYS=100`.

Strategy config:

- `gross_exposure=1.0`.
- `max_concurrent_positions=10`.
- Vol parity on 30-day vol with 30% annualized floor.
- Max position weight 0.30.
- Annual vol target 0.60, scale clamp `[0.30, 1.25]`.
- Weekend multiplier 1.5.
- Exit cooldown 7 days.

### Long Exit Logic

Venue-managed exits:

- ATR stop multiple 1.5.
- ATR take-profit multiple 4.0.
- Max hold 3 days.

The cycle handles time-stop fall-through with reduce-only market exits. Paper
marks exits to live ticker when available.

### Reconstruction Boundary

`_v11a_long_native_config()` has `require_full_pit_universe=False`; runs using
partial PIT inputs must be labelled. Current internal evidence is positive but
still depends materially on take-profit tail winners.

## Backtest Integrity

Any backtest that touches these profiles must declare decision timestamps, data
availability, order timing, fill model, exit state, PIT universe handling,
costs/funding, ledger path, and run label.
