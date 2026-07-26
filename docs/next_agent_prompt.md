# Next agent prompt

Copy everything below the line into a fresh session in this repository.

---

You are executing an **operator-ordered replacement of the deployed CONTINUOUS
system** with the redesigned single-component shape selected on 2026-07-26.
This is implementation work, not research: the decision is made, the evidence
is recorded, and your job is to make the deployed profile, the engine, the
runtime producer, and every downstream consumer agree on the new shape —
correctly, test-covered, and with the change point recorded.

## Authority and its limits

- **Decision**: operator override, 2026-07-26 session, verbatim intent: replace
  the deployed CONTINUOUS system with the redesigned shape. This overrides the
  default recommendation in `docs/continuous_redesign_2026-07-26.md` §3 (which
  was: register as a forward Lane-2 candidate first, deployed shape as
  control). Record it as exactly that in the promotion note — an operator
  override, not a rolling-record promotion.
- **What the override does NOT change**: `REAL_MONEY` stays false; mainnet and
  real-money credentials remain unauthorized; push remains CI-only (commit
  locally on `main`, never push); the actual rollout is dispatched by the
  owner through the guarded GitHub workflow and requires a locally and
  directly venue-flat account plus unexpired demo rule receipts (`STATE.md`,
  Standing operational constraints). You prepare the commit; you do not
  deploy.

## Read first

`AGENTS.md`, `docs/continuous_redesign_2026-07-26.md` (the evidence and the
hedged parity table), `docs/carry_hold.md` §for context on the portfolio role,
`docs/governance.md`, `STATE.md`, and `docs/repository_map.md` to navigate.
The redesign artifacts live at
`~/SHARED_DATA/bybit_full_pit/reports/continuous_redesign_2026-07-26/`
(V0–V10 engine reports plus `hedged_parity_summary.json` and
`hedged_V3_proposal.csv` — your render-parity target).

## The target shape, exactly

Replace the three ensemble components with **one**:

- `entry_event_trigger="turn3_pop3"`, `age_days_min=240`,
  `take_profit_pct=0.12`, `stop_loss_pct=0.35`, weight **1.0**.
- **New admission rule — the only new logic**: a candidate is admitted only if
  its **last settled funding rate ≥ 0** at the signal bar ("only fade pumps
  whose longs are paying"). Settled rate means a historical, already-applied
  print — never a predicted/next rate. **Unknown funding admits** (this
  matches the research basis that produced the numbers; count and journal
  unknown-funding admissions so the forward record can revisit that choice).
- Everything else unchanged: side short, decile 9, rmom-low quartile,
  BTC 30d uptrend gate **unchanged at > 0** (loosening it was measured and
  rejected three ways — V1/V2/V4), `entry_delay_hours=1`, `hold_hours=24`,
  crowd-2, cooldown as-is, inverse-vol sizing, `gross_exposure=0.5`,
  `max_active=25`, BTC+ETH hedge overlay and btcvol intensity regime as-is.
- New profile revision, suggested: `active_single_fund0_tp12_sl35_v1`. The
  revision bump is the recorded change point.

Expected performance of the new shape (hedged render, 2023-03→2026-07, from
the parity table — put these numbers in the promotion note, including the
trade-off): total +13.72%, max DD −1.69%, Sharpe 1.72, MAR 2.43, versus the
deployed ensemble's +15.85%, −2.85%, 1.84, 1.66. The operator chose the
drawdown/MAR/simplicity side of that trade; do not spin the Sharpe delta away.

## Implementation checklist

1. **Engine field** — `ContinuousEventConfig` gains
   `funding_min_at_entry: float | None = None` (None = off, exact current
   behavior). Enforce it in the engine's admission path using the funding
   data the engine already loads (`funding_lookup` in `_prepare_inputs` /
   `_run_trades` in `liquidity_migration/continuous_events.py`): the last
   settlement at-or-before the signal timestamp. **Known consequence**:
   adding a dataclass field shifts `config_hash()` and `kernel_strategy_id`
   for every CONTINUOUS config — that is why this ships as one deliberate
   change point. On the first post-deploy cycles the sizer's authoritative-
   chain self-heal (`ddbded5`, see `STATE.md`) will rebase prior-epoch state;
   expect and verify the healing telemetry rather than treating it as an
   incident.
2. **Profile** — `liquidity_migration/continuous_profile.py`:
   `ACTIVE_CONTINUOUS_COMPONENTS` → the single component above;
   `funding_min_at_entry=0.0` carried in the profile/config plumbing so
   runtime and reconstruction share one source of truth; revision bump.
3. **Runtime producer** — the demo/paper CONTINUOUS producer
   (`continuous_demo*.py` path) must apply the same admission check at
   candidate construction, from a **live settled-funding source** (the
   account stack already consumes funding for reconciliation — find the
   authoritative feed; do not add a parallel ad-hoc fetcher without checking
   what exists). Same unknown-admits semantics, with a per-cycle counter in
   the cycle telemetry.
4. **Downstream consumers** — sweep and fix: `WINNER_WEIGHTS` and the
   component loops in `scripts/continuous_deployed_equity_refresh.py`,
   operational-profile weight validation (weights must sum to 1.0 with one
   component), the equity-refresh parity gate (extend it to assert modeled
   `funding_min_at_entry` = profile value, like it asserts the stop),
   `continuous_component_sources` artifact cells, monthly trade counts, and
   every test that assumes three components. Search broadly before editing
   (`rg turn4p3`, `rg WINNER_WEIGHTS`, `rg ACTIVE_CONTINUOUS`).
5. **Render parity** — run the full standard render
   (`scripts/equity_curves.sh --sleeves continuous --start 2023-03-13
   --end 2026-07-17 --out <isolated dir>`) with the new profile and compare
   against `hedged_V3_proposal.csv` / the parity summary. Small deltas are
   expected (the research admission used the cross-venue panel's funding
   column; the engine uses the root's funding dataset) — reconcile and
   explain any material gap before committing; do not shrug it off.
6. **Tests** — unit tests for the new admission (sign boundary at exactly
   0.0, settled-not-predicted semantics, unknown-admits, telemetry counter),
   profile validation (single component, declared stop still mandatory),
   parity-gate extension, plus the full gates:
   `.venv/bin/python -m pytest -q`, ruff, mypy, `scripts/dev.sh check`.
7. **Records** — in the same commit: the five-line promotion note
   (`docs/governance.md` format; Decision line: "operator override
   2026-07-26, replacing rolling-record promotion") placed in
   `docs/strategy_program.md` with the change point; update the
   `docs/continuous_redesign_2026-07-26.md` status; update `STATE.md`'s
   local-candidate section (it is a local candidate until the owner's rollout
   lands — do not claim deployment); note that the existing sleeve kill
   criteria (`docs/preregistration/sleeve_kill_criteria_2026-07-20.md`)
   continue to govern the sleeve and that the new revision's forward evidence
   run restarts at this commit.
8. **Commit locally on `main`. Do not push.** Tell the owner the exact
   rollout preconditions from `STATE.md` (venue-flat account, valid demo rule
   receipts) and that the workflow dispatch is theirs.

## How to work

Preserve any unrelated user work in the tree. Read every file before editing
it; follow existing idioms; comments only where the code cannot say it. When
something you find contradicts this prompt (a consumer this checklist missed,
a funding feed that does not exist where expected, a parity gap you cannot
explain), stop and report rather than improvising around it — this prompt is
fallible; the code and `STATE.md` are the authority. Report the outcome with
the honest numbers, including what you did not do.
