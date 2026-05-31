# Pre-registration — promote age300 + ff6 to the live demo `promoted` profile

**Date:** 2026-05-31
**Author:** claude (operator-directed: "get this promoted on the demo … all the features we just tested, ff6 and the age gate")
**Stage:** accepted — demo/paper forward-test candidate (NEVER real money; the forward demo is the Tier-3 arbiter)
**Run label:** VALIDATED (the in-sample evidence MAY be cited in this demo-promotion decision)

## What's changing

The deployed short-sleeve `promoted` profile (`event_demo._demo_event_config`, the
`profile == "promoted"` branch) gains the **age SELECTION gate** and the **ff6_4pct
failed-fade EXIT** on top of the existing drop_all_4 package. Old → new:

| field | old (drop_all_4) | new (drop_all_4 + age300 + ff6) |
|---|---|---|
| `liquidity_migration_pit_age_days_min` | 90 | **300** (age gate, E2) |
| `failed_fade_exit_hours` | 0 (off) | **6** |
| `failed_fade_loss_pct` | 0.0 | **0.04** |
| `failed_fade_min_mfe_pct` | 0.0 | **0.01** |
| `failed_fade_close_location_min` | 1.0 | **0.0** |

Unchanged: drop_all_4 (max_active=12, the 4 dropped vetoes, universe_rank_max=99999),
take_profit=0.26, all entry-quality filters. `strategy_id` is **kept**
(`liqmig_union_q40_h3_tp26_g100_qsqueeze`) → the forward ledger stays continuous and the
**deploy date is the clean pre/post split** (same convention as drop_all_4; avoids
orphaning live open positions).

## Why these two features

- **age (pit_age≥300):** the robust, cross-venue-validated SELECTION refinement (E2).
  Removes idiosyncratically-strong *young* names (the bull-squeeze cohort). On the standard
  baseline it is all-thirds-positive on Bybit and rescued Binance-recent from −39%→≈flat.
- **ff6_4pct (failed-fade exit):** a pure loss-mitigation EXIT — cuts squeeze-shorts that
  never worked (held ≥6h, MFE <1%, loss >4%) at ~6–8h instead of letting them ride to the
  −12% hard stop. NOT a profit source; a drawdown/loss tamer.

## Live==backtest fidelity (the robustness gate)

- **ff6:** the live `_failed_fade_exit_since_entry` (event_demo.py) is **logic-identical**
  to the backtest `_failed_fade_exit_hit` (volume_events.py) — same guard (hours>0, loss>0),
  same MFE<min_mfe test, same close_return≤−loss test, same close-location rule. Verified
  field-by-field 2026-05-31.
- **age:** applied by the SAME filter in both paths (`_filter_liquidity_migration`,
  volume_events_filters.py).
- **deploy guard:** `scripts/deploy_vps_live.sh` now asserts the live `promoted` profile has
  `pit_age==300` + the 4 ff6 knobs (and `test_runtime_scripts` pins those asserts), so the
  live gate/exit cannot silently drift from this receipt.

## Evidence — A. standalone (age/ff6 on the standard baseline, full-PIT, 45bps×3, bar_extreme_capped)

`docs/preregistration/age-ff6-no-rmom-2026-05-31.md` (just run). ff6 is NOT inert on the age
book (it was rmom, not age, that neutralized it in the combined run):

| venue | cell | trades | return | maxDD | Sharpe | MAR | `failed_fade` |
|---|---|---:|---:|---:|---:|---:|---:|
| bybit (funding real) | age | 583 | +70.97% | −17.68% | 1.30 | 1.05 | 0 |
| bybit (funding real) | **age_ff6** | 585 | **+78.51%** | **−17.43%** | **1.16** | — | **29** |
| binance (funding blind) | age | 307 | +21.95% | −12.59% | 0.71 | 0.52 | 0 |
| binance (funding blind) | age_ff6 | 307 | +23.09% | −14.20% | 0.75 | 0.48 | 21 |

ff6 ADDS on Bybit (decisive, funding-real); ~neutral/slight-DD-hit on funding-blind Binance.

## Evidence — B. the ACTUAL deploy config (drop_all_4 + age300 + ff6, both venues)

This is the config being deployed (age+ff6 stacked on drop_all_4's wider pool, NOT the
standard baseline of part A). Pre-registered comparison vs drop_all_4-alone; tag
`promote_drop4_age_ff6_2026-05-31`. **Hypothesis:** age300 thins drop_all_4's unbounded-rank
tail of young names → expect Bybit held-or-better AND a Binance rescue (drop_all_4 was
−12.2% on Binance; age rescued Binance standalone).

> RESULTS — filled post-run (numbers below; §A/the plan above are pre-committed).
> Tag `promote_drop4_age_ff6_2026-05-31`, 6 cells, all `run_label=full_pit_universe`. The
> `drop4` control reproduces the drop_all_4 receipt EXACTLY (bybit 820 trades / +56.9% /
> −25.2%) → override fidelity confirmed. 16 GB box, serial, `POLARS_MAX_THREADS=6`.
> MAR = annualized-return / |maxDD| over the 3.15y span.

| venue | cell | trades | return | maxDD | Sharpe | MAR | `failed_fade` |
|---|---:|---:|---:|---:|---:|---:|---:|
| bybit (funding real) | `drop4` (current live) | 820 | +56.9% | −25.2% | 0.89 | 0.61 | 0 |
| bybit | `drop4 + age300` | 598 | +90.8% | −14.1% | 1.46 | 1.61 | 0 |
| bybit | **`drop4 + age300 + ff6`** | 598 | **+96.3%** | **−13.7%** | **1.56** | **1.74** | **29** |
| binance (funding blind) | `drop4` (current live) | 509 | **−12.2%** | −38.9% | −0.23 | **−0.10** | 0 |
| binance | `drop4 + age300` | 307 | +17.2% | −15.2% | 0.57 | 0.34 | 0 |
| binance | **`drop4 + age300 + ff6`** | 307 | **+19.1%** | −16.7% | 0.63 | **0.34** | **21** |

**THE HEADLINE — age300 fixes drop_all_4's cross-venue failure.** drop_all_4 alone FAILED the
Tier-2 cross-venue guard (binance −12.2% / MAR −0.10 — its documented weakness). Adding age300
flips binance to **+17.2%** AND lifts bybit +56.9%→+90.8% with DD nearly halved (−25.2%→−14.1%).
Mechanism: drop_all_4 unbounds the rank ceiling, admitting a young-name squeeze-prone tail;
age300 strips exactly those 222 names (820→598 bybit, 509→307 binance) — the drawdown-drivers.
The stack is **cross-venue positive on BOTH venues** — it CONVERTS a Tier-2-failing profile into
a Tier-2-passing one.

**ff6 increment** (`drop4_age` → `drop4_age_ff6`): bybit MAR 1.61→1.74, ret +90.8%→+96.3%, DD
slightly shallower, 29 catches (R13 loss-mitigation pattern); binance ~flat MAR (21 catches, DD
slightly deeper) — same neutral-on-funding-blind-binance read as §A. Net: ff6 ADDS on Bybit,
neutral on Binance.

**All-weather (net-return summed, split 2025-06-01):** bybit EARLY +47.7% / RECENT +23.8%
(both strongly positive); binance EARLY +23.4% / RECENT −4.0% (early-strong, recent-soft — the
known age-gate Binance caveat, funding-blind). NOT a recent-only artifact (bybit strong in both;
binance carried by a strong early half). Full-window both venues positive.

**VERDICT: GO.** Bybit MAR 0.61→1.74 (far better, not worse → GO rule satisfied); ret +96.3%
(positive). Binance rescued from −0.10 to +0.34 (the upside case). Deploy the stack to demo+paper.

## Decision rule (pre-committed)

Tier-2 demo-candidate, MAR-primary. This promotion deploys to **demo + paper only**; the
forward demo/paper ledger is the only Tier-3 (real-money) arbiter — nothing here promotes to
real money.

- **GO** (deploy the stack) if, on the actual deploy config (part B): Bybit MAR is **not
  worse** than drop_all_4-alone (age+ff6 must not degrade the lead venue) AND the change does
  not turn Bybit return negative.
- **Binance** is informative, not blocking (funding-blind + drop_all_4's known weakness); a
  Binance *improvement* from age300 is the upside case but not required for GO.
- **NO-GO / reconsider** if age+ff6 materially *hurts* Bybit on the drop_all_4 pool → do not
  stack; surface and re-scope (e.g. promote age+ff6 on the standard baseline instead).

## Forward review (a priori)

Track the forward demo cross-venue from the deploy date. Bybit is the lead. Watch: (1) does
ff6 fire live at the expected rate (~5% of trades) and cut losers as modeled; (2) Binance
direction; (3) age gate trade-count reduction matches backtest. Revert criteria unchanged
from drop_all_4 (Bybit lead must hold).

## Deploy mechanics

Deploy = `git push` to main → GitHub Actions `vps-deploy.yml` auto-runs
`scripts/deploy_vps_live.sh` via SSH → pytest smoke subset + the strategy-settings assertion
block (now updated for age300+ff6) → restarts the 6 systemd daemons. Pre-push gate (CI
parity): `ruff check liquidity_migration tests` + `pytest -q` MUST pass locally first.
strategy_id kept → deploy date = pre/post split. No real-money toggle touched (demo stays demo).

## Provenance / changed files

- `liquidity_migration/event_demo.py` — `_demo_event_config` promoted branch (+age300, +ff6).
- `scripts/deploy_vps_live.sh` — promoted-profile assertions (pit_age==300, ff6 knobs).
- `tests/test_runtime_scripts.py` — golden-test pins the new deploy asserts.
- `tests/test_liquidity_migration_event_demo_cycle.py` — `test_promoted_profile_carries_drop_all_4_age300_and_ff6`.
- Engine gates unchanged (age `volume_events_filters.py`; ff6 backtest `volume_events.py`,
  live `event_demo.py` `_failed_fade_exit_since_entry`).
