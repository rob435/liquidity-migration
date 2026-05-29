# Research plan — SELECTION vs EXECUTION (the fade strategy)

**Created 2026-05-29.** Narrow, falsifiable forward plan, run on the **5950X**.
Supersedes the retired Round-2 R0–R12 / C0–C3 phase program (findings consolidated
in [research_summary.md](research_summary.md); the R-phase dispatchers were deleted —
git history has them). Methodology standard:
[backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).
Decision rule: the three-tier demo-arbiter in [STATE.md](../STATE.md) (MAR-primary,
forward demo is the arbiter, Tier-3 real-money gate stays strict).

## Thesis (one paragraph)

The strategy is **two separable signals**. **Selection** — a liquidity-migration
event picks a *candidate pool* (a mid-liquidity perp takes price-insensitive flow).
**Execution** — that in-migrated flow exhausts and **fades**; you short the
**confirmed fade** (pop → giveback), not the top. This is a **fade strategy, not a
catch-the-top strategy.** The momentum-continuation at the extremes (names continue
*up* before fading) is not evidence of failure — it is *the reason the execution
signal must wait for confirmation*. The old "Round 2 = null" measured a single
worst-case execution (immediate entry + worst-case wick fills + over-concentration)
and blamed the signal. This plan tests the two signals **separately**.

## Why this plan is narrow

We are not re-running the daily strategy's whole parameter space. We have one core
question — **does the execution signal carry alpha independent of selection?** — and
two follow-ons that only matter if the answer is yes. Three experiments, sequenced so
the cheapest and most diagnostic runs first. Failure of E1 is a real, informative
result, not a dead end.

---

## E1 — Quantify the execution signal (decisive, cheap, FIRST)

**Question.** On the *same* selection (the daily liquidity-migration candidate pool),
how much of the alpha is the **execution** signal? i.e. does fade-confirmation entry
beat near-immediate entry?

**Method.** Hold selection + costs + concentration fixed at the realistic baseline
(`--stop-fill-mode bar_extreme_capped`, 10% cap; `--max-active-symbols 12`; honest
15 bps; full-PIT; **both venues**). Vary only the entry policy:

| arm | flag | meaning |
|---|---|---|
| A (control) | `--entry-policy fixed_delay` | near-immediate entry after the signal close |
| B (treatment) | `--entry-policy promoted_quality_squeeze` | wait for pop → giveback ("fade the fade") |

Then characterize B's edge shape by sweeping its knobs (one at a time, small grids):
`--entry-quality-squeeze-pop-bps`, `--entry-quality-squeeze-giveback-bps`,
`--entry-quality-squeeze-wait-hours`, `--entry-quality-squeeze-h1-return-bps`,
`--entry-quality-squeeze-h1-close-location-min`.

**Read-out.** Per-venue MAR / total return / max DD / Sharpe / trades for A vs B; the
B−A delta is the execution premium. Apply the Tier-2 demo-candidate rule to B (and to
the best squeeze-knob cell) vs the A control.

**Falsifier (a real result either way).** If B ≈ A (no execution premium on either
venue), the alpha is **selection-only** and the fade-confirmation framing is not
load-bearing — document it and pivot E2/E3 toward selection, not execution. If B
materially beats A cross-venue, the execution signal is real and E2/E3 are justified.

**Cost.** Cheap — existing engine, no new code. ~2 full-PIT cells + a small knob grid
per venue. **Run this first.**

---

## E2 — Apply the execution layer to the CONTINUOUS candidate pool

**Gated on E1 = execution premium is real.** (If E1 says selection-only, replace E2
with a selection-refinement study instead.)

**Question.** The continuous candidate signal carries real, robust cross-venue
**selection IC** (c1 precheck: composite ≈ −0.08 both venues at 24/72/168h; `rv_168h`
strengthening) but was only ever tested with **immediate** entry (c2 → "not
tradeable"). Does the **fade-confirmation execution** rescue it — i.e. is c2's null a
timing-the-top artifact, exactly like the daily case?

**Method.** Extend the continuous candidate path (the c1/c2 rolling-feature pipeline)
to emit candidate events, then run the **same pop-then-giveback execution** on those
candidates. Compare immediate-entry (c2 baseline) vs fade-confirmation on the
continuous pool, both venues. This is the larger, code-touch experiment — it builds
the C0 continuous engine the c1 precheck was gating (c1 estimated ~5–7 days). Reuse
the existing `_quality_squeeze_entry_decision` logic; do not fork it.

**Falsifier.** If fade-confirmation does not move the continuous pool to Tier-2 on
both venues, the continuous variant stays a documented null — but now an *honest* one
(it got the execution layer the daily strategy has), not a timing-the-top artifact.

**Cost.** Medium–large (engine work). Pre-register the build scope before starting.

---

## E3 — Sniper: sub-1h execution refinement

**Gated on E1/E2 = execution timing matters AND a sub-1h data path is available.**

**Question.** Does finer (sub-1h / 1m) confirmation timing improve the execution edge
(tighter giveback detection, less entry lag) without overfitting?

**Method.** Implement a `tiered_execution_sniper` entry policy (the CLI `--entry-policy`
help already names it, but `ENTRY_POLICIES` does **not** yet include it — it needs
wiring + a 1m/sub-1h confirmation path). This is execution refinement on a **fixed**
selection: same candidate pool, finer entry. Compare against the 1h
`promoted_quality_squeeze` winner from E1/E2.

**Falsifier.** If sub-1h timing does not beat the 1h squeeze cross-venue, the 1h
confirmation is sufficient and the sniper is dropped — no sub-1h infra is deployed.

**Cost.** Large (new entry policy + sub-1h data path). Pre-register before starting.

---

## Sequencing & gating

```
E1 (cheap, decisive) ── execution premium real? ──► E2 (continuous + execution)
                          │                              │
                          └─ no: pivot to selection      └─ timing matters? ──► E3 (sniper)
```

E1 runs now. E2 only if E1 shows a cross-venue execution premium. E3 only if E1/E2
show entry *timing* (not just confirmation) is the lever. Each gate that fails gets a
one-paragraph negative-trigger note — do not look for excuses to run a gated phase.

## Decision rule (unchanged — do not re-derive)

Three-tier demo-arbiter from [STATE.md](../STATE.md): Tier-1 investigation (keep
studying), Tier-2 demo-candidate (→ forward demo; **return positive both venues +
pooled MAR Δ > +0.1 + neither venue worse than −0.5 MAR + ≥30 by / ≥20 bn trades**;
fragility diagnostics reported, non-blocking), Tier-3 real-money (STRICT, not
loosened). MAR-primary, Sharpe-secondary. `scripts/r1_robustness.py --sweep-tag <TAG>`
emits the Tier-2 verdict + fragility from per-cell ledgers. **No further loosening to
rescue a near-miss.**

## Foundation: the R4 risk model (Tier-3 residual-Sharpe)

The Tier-3 real-money gate requires **residual Sharpe ≥ +0.3 (factor-model residual)** —
i.e. a cell's alpha must survive after stripping exposure to known factors. That machinery
is **already built, validated, and live on `main`**: `liquidity_migration/risk_model.py`
holds a validated **6-factor** model (`btc_beta`, `xs_rank_ret_30d`, `realized_vol_rank`,
`funding_rate_z`, `liquidity_rank`, `premium_index_z`) plus `decompose_strategy_pnl`, which
takes a cell's trade ledger and returns its residual after factor decomposition. It passes
an honest within-day permutation-null variance-capture test (p=0.0 both venues) — not the
in-sample R²≥0 tautology. Validation record:
[preregistration/r4-risk-model-verdict.md](preregistration/r4-risk-model-verdict.md).

So R4 is **not an experiment in this plan** — it is the *foundation under* the Tier-3 gate
that E1/E2/E3 must eventually pass. Do not rebuild it. At the Tier-3 gate, residualize each
demo-candidate cell on this model (`decompose_strategy_pnl`) and require residual
Sharpe ≥ +0.3; a cell that fails is "selling vol / buying beta," not carrying alpha.

## 5950X operating notes

- One full-PIT `volume-events` cell peaks **~23 GB** → run sweeps at
  `SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8` (8 workers OOMs the box; `_sweep_runtime.py`
  is memory-aware and auto-caps, but set it explicitly). Light (non-full-PIT) sweeps
  may use `SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4`.
- After any OOM/kill, clear `<root>/.locks/*.lock` before re-dispatching.
- Per-venue full-PIT roots: `~/SHARED_DATA/{bybit,binance}_full_pit` (see
  [data_roots.md](data_roots.md)). **Cross-venue agreement is the robustness bar** —
  always run both venues; a single-venue edge does not clear Tier-2.
- Dispatch a single cell with `scripts/volume_events_cell.sh` (fills the production
  baseline flags); multi-cell sweeps with a `scripts/_sweep_runtime.py`-based
  dispatcher (write a new one per experiment; the old R-phase dispatchers are gone).

## Pre-registration discipline

Each experiment gets a dated receipt under `docs/preregistration/` **before** the run
(params, falsifier, decision rule), committed in the same PR as any code change —
per [parameter_pre_registration.md](parameter_pre_registration.md). `EXPLORATORY` runs
are allowed but must not be cited as promotion evidence. Write the verdict after each
experiment and roll a one-paragraph update into [research_summary.md](research_summary.md).

## Out of scope (do not re-open)

- **Re-litigating the daily parameter space.** The realistic re-baseline already shows
  the daily strategy gross-positive both venues; the open question is *execution*, not
  more daily filter mining.
- **Long-sleeve standalone alpha.** Exhaustively searched → FC is the ceiling
  ([long-sleeve-alpha-search-null]). The long sleeve's value is a low-correlation
  *overlay* on the short book ([long_short_overlay_findings.md](long_short_overlay_findings.md)),
  not a return engine. Do not mine it again.
- **Further loosening the decision rule.** It was loosened once, on principle,
  pre-registered. The Tier-3 real-money gate is not negotiable.
