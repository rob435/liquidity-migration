# Active Trading Logic

Last manually reconciled with code and deploy files: 2026-07-13. Verify the
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
3. `deploy/systemd/*.service` and the account execution environment for the
   repository process topology. The capture marker authorizes a bounded evidence
   window; the separate evidence-bound deploy authorization is still an operator gate, not proof
   that the host passed it.
4. `liquidity_migration/account_service.py`, `account_kernel.py`,
   `account_service_runner.py`, `account_reconcile.py`,
   `account_venue_accounting.py`, and `account_paper_runner.py` for execution,
   accounting, risk and notification ownership.
5. `liquidity_migration/promoted.py` for registry objects.
6. `liquidity_migration/continuous_demo.py` and
   `liquidity_migration/continuous_forward_replay.py` for continuous runtime and
   replay config.
7. `liquidity_migration/long_native_event_demo.py` for long v11a runtime config.

## Running Surface

The repository ceiling in `deploy/sleeves.env` currently requests:

| Process group | Gate | Runtime role |
| --- | --- | --- |
| Demo account owner | capture marker + account env | sole Bybit mutator; position/funding reconciler; risk/protection and Telegram owner |
| Paper account owner | capture marker + paper account env + passing calibration | sole deterministic paper execution/accounting owner |
| Long demo + paper | `LONG_SLEEVE=on` | signal engines that publish component targets |
| Continuous demo | `CONTINUOUS_SLEEVE=on` | signal engine that publishes component targets |
| Continuous paper | `CONTINUOUS_PAPER_SLEEVE=on` | signal engine that publishes paper component targets |
| Continuous hedge | continuous demo gate + timer | BTC/ETH target calculator and publisher |

Sleeves do not own credentials, venue orders, fills, P&L, protection repair or
Telegram. Demo targets are executed only by the account owner. Paper targets
are advanced only by the shared execution twin against its independent L2
capture. Canonical account journals are authority; sleeve Parquet is signal or
compatibility telemetry. See `docs/account_execution_cutover.md` for the open
acceptance gate before enabling this repository surface on a host.

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
- Adverse-reduction pause: 8 account/symbol loss batches inside 1440 minutes,
  counted once per canonical P&L key rather than once per component row. The
  inherited threshold has not yet been re-estimated under this new counting
  unit, so it is a prospective demo/paper guardrail rather than a validated
  optimum.

### Components

| Tag | Trigger | Age floor | TP | Weight |
| --- | --- | ---: | ---: | ---: |
| `p3` | `turn3_pop3` | 240d | 12% | 0.3333333333333333 |
| `p4p3` | `turn4_pop3` | 240d | 12% | 0.2222222222222222 |
| `p4p5` | `turn4_pop5` | 240d | 12% | 0.4444444444444444 |

### Sizing

Raw component target notional before account-kernel venue discretization:

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

The target route requires a healthy, recent account-owner capital observation
and canonical component state. The sleeve uses those read models for sizing and
held-name decisions, then atomically publishes desired component targets. It
has no private venue client and cannot accept, submit, adopt, repair or close an
order.

The account kernel evaluates each target batch against the current account risk
snapshot, explicit absolute risk policy and fresh demo-verified instrument
rules. Quantity step, minimum quantity, minimum notional, maximum order size and
leverage constraints live there, not in a sleeve. A rejected batch is durably
journaled with stable rejection keys. The demo owner reconciles REST and private
execution events, and fails health closed on mismatch, stale market data,
missing rules or missing native protection. Paper uses the same kernel and rule
rows with the deterministic execution twin.

Demo, paper and historical account execution use the shared canonical journal
and reducer described in `docs/canonical_execution_journal.md`. Old mutable
sleeve lifecycle rows are compatibility input for archived roots only; they are
not current position, fill or P&L authority.

Archived pre-cutover roots may contain
`reports/event-risk-ws/stop_audit_events.jsonl`, written by the retired
`ws_risk` process. Reconciliation can retain those rows as historical repair
evidence. No active service writes that path and it has no routing authority.

The former sleeve-local portfolio-heat and account-drawdown overlays are removed
from the target producer. Account-wide exposure, margin and drawdown authority
belongs to the serialized account kernel under an explicit risk-policy file.
Those limits are capital controls, not backtest-parity knobs, and must be chosen
from explicit ruin/exposure constraints before any real-money discussion.

### Continuous Exit Logic

Active exits:

- After the entry is fully and unambiguously filled, the account protection
  engine derives the component's 12% take-profit from confirmed fill VWAP and
  turns its target to zero when crossed.
- The sleeve publishes a zero component target at `max_hold` after 24 hours;
  the clock starts at the first attributable fill, and only the account owner
  may translate the target into a venue order.
- The strategy has no component stop.

The demo account owner separately requires an explicit exchange-native disaster
stop for every reconstructed net position. That process-death seatbelt is an
account safety control, not a strategy stop and not part of the research P&L
claim. A stopless component remains acceptable only inside the current
demo/paper authorization; it is not a mainnet precedent.

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

The periodic hedge process reads the canonical CONTINUOUS component book and a
recent healthy owner equity observation, calculates absolute BTC/ETH targets,
and publishes them to the same account inbox. It owns no venue client or hedge
ledger. There is no fixed strategy-side resize floor; executable quantities are
decided from the verified symbol rule row by the account kernel.

The former quarter-size PostOnly sell at +8% was rolled back on 2026-07-10
after it added loss into the forward 1000TAGUSDT squeeze without paper/backtest
parity. Its config, placement, cleanup, notification, CLI and service wiring are
removed from the future runtime; only archived link/ledger attribution remains.
The account-owner cutover must prove venue-flat positions and zero regular or
conditional orders before an empty/new journal may start.

The rejected dynamic-exit paper shadow was retired on 2026-07-13. It never had
order authority; its negative decision record remains in the research history,
but current demo and paper cycles no longer run or persist that experiment.

### Reconstruction Boundary

The official local continuous replay target is `FROZEN_FORWARD_CONFIG`: three
components, inverse-vol component sizing, 24h hold, 12% component TP, no stop,
BTC+ETH hedge, and BTC-vol regime.

The daemon adds target-publishing behavior, paper/demo state, and the BTC-risk
sizing overlay. A frozen component-ledger
backtest is therefore not a literal daemon replay unless it explicitly
implements those state machines and the account execution model.

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

Component exit thresholds:

- ATR stop multiple 1.5.
- ATR take-profit multiple 4.0.
- Max hold 3 days.

The target records relative stop/take-profit fractions determined from the
signal's ATR. After the entry is fully and unambiguously filled, the account
protection engine derives executable thresholds from confirmed fill VWAP and
publishes a zero component target when one is crossed. The demo owner also
installs one exchange-native net disaster stop using the outermost applicable
component stop (lowest for a long, highest for a short), never the tightest.
The sleeve publishes a zero target three days after the first attributable
fill. Only the account owner turns aggregate target changes into venue orders;
paper uses the shared execution twin rather than sleeve-local idealized fills.

### Reconstruction Boundary

`_v11a_long_native_config()` has `require_full_pit_universe=False`; runs using
partial PIT inputs must be labelled. Current internal evidence is positive but
still depends materially on take-profit tail winners.

## Backtest Integrity

Any backtest that touches these profiles must declare decision timestamps, data
availability, order timing, fill model, exit state, PIT universe handling,
costs/funding, ledger path, and run label.
