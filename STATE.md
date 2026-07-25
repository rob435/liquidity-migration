# Operational State

Current operational snapshot. **Exact live truth comes from the authenticated
deployment receipt and `scripts/ops.sh status`, not from this prose.** Every
observation below is point-in-time and dated; none of it is a claim about the
account right now.

Detailed incident evidence lives in `docs/audit/`. This file records what is
deployed and what constrains it — not the history of how it got there. That
history is in Git and in the audit receipts indexed at the bottom.

## Deployment

- **Installed implementation commit: `bf3b6b6f0572e8bc34e7d0c1834130402778f933`**,
  deployed from canonical `main`, profile `operational`, on 2026-07-25 via the
  guarded `rollout` dispatch (Actions run 30159709018) after an earlier
  `verify`-mode dispatch correctly refused the stale `a9ac75d1` checkout. The
  rollout proved pre-stop and stopped venue flatness, installed, issued fresh
  authority, and activated; an independent read-only check the same hour
  returned `verify-ok commit=bf3b6b6… profile=operational` with demo order
  permission intact. **Change point: this deploy makes the declared CONTINUOUS
  35% component stop live** (profile revision `active_tp12_sl35_v1`; anomaly
  research §20.1) — the 2% account fallback is no longer CONTINUOUS's de facto
  exit rule.
- **2026-07-25 CONTINUOUS entry block, resolved.** Every cycle from 2026-07-22
  22:24 UTC (1,502 cycles) blocked all CONTINUOUS entries with
  `accepted_state_invalid`: `btc_risk_sizing_state.parquet` survived the
  2026-07-22 ledger reset while the journal evidence it references was
  archived away, and the sizer fails closed on unverifiable state — the BTC
  trend gate and funnel were never the blocker. Remediation: both stale state
  files (demo + paper) retired in place with sha256 receipts, and the reset
  flow now retires this account-epoch state unconditionally at the epoch
  boundary (this commit's change to `scripts/reset_demo_paper_ledgers.sh`).
  The empty forward CONTINUOUS record over those three days is a block
  artifact, not market evidence.
- Boundary: **`DEMO=true`, `REAL_MONEY=false`.** Mainnet, `REAL_MONEY`, and
  real-money credentials remain unauthorized.
- Installed demo and paper operational-profile bytes are identical, SHA-256
  `cf68369c587c4eb736b5e63f9524a15eb125daa820f09c4167de49aac9fcac18`. The
  tracked editable source is `configs/operational.demo.json`.
- Deployment status is authoritative only when tied to an exact pushed commit and
  a fresh authenticated rollout receipt.

### Local candidate — not committed, not deployed

A 2026-07-23 deployment-automation candidate exists in the working tree and
**grants no mainnet or `REAL_MONEY` authority**. It parallelizes the paper and
demo rollout trees (modelled critical path 102s instead of 200s), reuses a
still-valid residual-momentum table instead of rebuilding, adds a create-only
rule projection that retains fresh evidence for an equal/safe-subset candidate
population, and fixes a safety-significant classifier defect where the combined
stale/future-dated loader error could send future evidence into the expiry
refresh path. Fresh, expired, and future-dated states are now distinguished from
one stable receipt snapshot; only genuine expiry is refreshable.

Local validation passed repository doctor, Ruff, mypy, shell/YAML checks, and the
full gate at `2,274 passed / 1 skipped`.

## Topology

Six persistent services plus three timers:

| Kind | Units |
| --- | --- |
| Account owners | demo, isolated-paper |
| Target producers | demo/paper × LONG/CONTINUOUS |
| Timers | continuous hedge, residual-momentum refresh, demo-paper liveness |

- Paper runs as the non-login `liquidity-migration-paper` user with private
  state, no demo/mainnet credentials, and byte-identical isolated candidate,
  rule, and risk inputs. Paper is explicitly `integration_only_uncalibrated`:
  its cycles are routing/lifecycle evidence, not performance or fill-quality
  evidence.
- Bulk collectors are removed and raw account-market persistence is disabled.
  Live L2 readiness and exact decision-book capture remain enabled.

## Risk envelope

The account owner caps leverage at **2×**, symbol notional at **5,000 USDT**,
component/account gross at **20,000 USDT**, and initial margin at **10,000
USDT**. The bound operational profile retains 2× entry leverage, a 0.5 LONG
notional multiplier, and a 1.0 CONTINUOUS multiplier. Startup and authorization
reject unknown profile fields, producer leverage above the owner cap, or
registered exposure envelopes outside the same profile.

LONG, CONTINUOUS, and hedge leverage plus exposure/risk knobs come from one
strict operational profile; independent systemd sizing variables were removed.

## Standing operational constraints

- **Rollout requires a locally and directly venue-flat account.** No
  flatten/cancel bypass is authorized. A failed verification is not permission to
  hand-start a partial fleet.
- **Demo rule receipts expire at a strict 168 hours.** An expired receipt blocks
  activation and makes rollback unavailable; rollout re-probes stale rules once
  the fleet is stopped and flat. The watchdog warns during the final 24 hours but
  cannot place orders or refresh authority.
- **Unknown safety-critical state fails closed.**
- Three CONTINUOUS candidates (`HIGHUSDT`, `PUMPBTCUSDT`, `WHITEWHALEUSDT`) have
  venue `deliveryTime=1784538000000`. They are recorded prospectively in private
  mode-0600 retirement registries and may retire only while account positions,
  targets, orders, and inbox exposure are all flat.
- Push remains CI-only. The manual GitHub workflow exposes guarded `rollout` and
  `recover` with explicit profile, task reference, demo/paper authorization, and
  reset-receipt inputs.

## Forward evidence stream

**The prospective runtime-parity epoch machinery was deleted on 2026-07-24 by
owner instruction** — the comparator, the epoch-start collector
(`forward_epoch_start`), and all eight registered contracts. Its published start
and verification receipts still exist on disk and on the VPS, but nothing in this
checkout reads, validates, or can reproduce them.

What remains is the plain rolling record, which is what the Progressive Evidence
Model in `docs/governance.md` actually calls for: each committed config is graded
on the run of days it predates, continuously, with recorded change points. There
is no ceremony, no waiting window, and no separate registration artifact — the
commit is the registration.

Two dates survive as a plain range because
`docs/preregistration/sleeve_kill_criteria_2026-07-20.md` measures against them:
**2026-07-19 14:00 UTC through 2026-10-17 14:00 UTC**, first 45 days
calibration-only. They are now just dates in an active contract, not a registered
epoch with its own tooling.

## Evidence boundary

The tracked hedge history is an **immutable sizing-only model prior through
2026-07-09**. It is not live-extended calibration or performance evidence.

The forward execution stream accumulates rolling evidence continuously under the
Progressive Evidence Model. At this snapshot **no committed config has a graded
forward record**, and this is not a strategy-alpha experiment. Promotion is a
five-line note plus a recorded change point when a rolling record earns it.

**Real money remains a separate owner door: no runtime status, paper result, or
rolling record opens it on its own.**

Research-only: Strategy Overhaul V2 closed with no qualifying thesis and did not
touch its reserved holdout; its diagnostic portfolios are model-based and
negative after costs/funding. The consolidated research conclusion and successor
direction are in `docs/strategy_program.md`; current anomaly evidence is in
`docs/anomaly_research_2026-07-24.md`. Retired receipts remain in Git history.

## Known benign alert shapes

Each was diagnosed to a root cause and fixed. Listed so an operator does not
re-diagnose a page that has already been explained; detail is in the linked
audit.

| Alert shape | Diagnosed cause |
| --- | --- |
| `unowned_venue_order` after a stop triggers | Owner disowning its own just-consumed Full stop while Bybit's open-order cache still lists it. Bounded 10-minute terminal-visibility grace, identity-evidence required. |
| `account funding reconciliation is stale: age_ns~4-5e9` | Report timestamped itself before its paginated REST queries, then held to the shared 4-second position bound. Documented 30-second funding floor. |
| `waiting for queue-head market data: X:stale_book` | Lost/rejected orderbook subscribe. Socket rebuilds after 30 frameless seconds for a new subscription. |
| `latest cycle is 0.1 min future-dated` / `future_book` | Local read/update races sampling wall time before the snapshot. Ordering fixed; true future timestamps still page. |
| `continuous-hedge.service FAILED` after an owner-health page | Duplicate of the owner-health root cause. A hedge run blocked by unhealthy owner health now exits 0 with a blocked receipt. |
| Negative owner-health ages | Strategy event time reused after concurrent heartbeats. Operational freshness now samples adjacent wall time. |

## Audit receipts

| Date | Subject |
| --- | --- |
| 2026-07-24 | `docs/audit/2026-07-24-repo-and-strategy-audit.md` — repository and strategy audit; measured tail geometry, funding inversion, data tiers |
| 2026-07-22 | `docs/audit/2026-07-22-demo-journal-publication-race.md` — journal publication race behind delayed fills and protection adoption |
| 2026-07-22 | `docs/audit/2026-07-22-deploy-workflow-and-runtime-followup.md` — rollout gate, expired-rule recovery, shell status defect |
| 2026-07-22 | `docs/audit/2026-07-22-paper-reduction-convergence.md` — paper reduce-only freshness contract mismatch |
| 2026-07-21 | `docs/audit/2026-07-21-account-kernel-incident.md` — unprotected-interval gap and the venue-stop-first repair |
| 2026-07-19 | `docs/audit/2026-07-19-load-bearing-audit.md` — ten runtime defect fixes and two O(history) scaling removals |

## Recovery archive

The 2026-07-22 owner-authorized full reset archived and verified all 22 selected
account journals, inboxes, captures, and strategy epoch projections before
clearing them. Recoverable archive:
`/opt/liquidity-migration/data/_archive/ledger-reset-20260722T213413Z-owner-authorized-full-reset-20260722.tar.gz`
(31,490,855 bytes; SHA-256
`e629df3efb8c0a3e5101479298589e23d65b7b95c9daa9859531a6da3f91c6d2`). Config,
persistent lock inodes, reports, caches, residual-momentum input, and root-level
market data were preserved.

A pre-evidence BTC-risk state file was rejected rather than migrated and archived
at `/var/lib/liquidity-migration/retired-state/20260716T0948Z-btc-risk-pre-evidence/`
(SHA-256 `be80dc76002dc8a0c943798e23b58c29f3894e83f9d6d7a72414008df1d9f146`).
