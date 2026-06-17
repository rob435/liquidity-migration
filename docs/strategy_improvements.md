# Strategy & Logic Improvement Proposals

**Operator-gated queue.** Anything that changes *which trades the system takes*,
*how it sizes them*, or *how a signal is computed* is written here as a proposal
— it is **not** applied to a live config or promoted profile without explicit
operator sign-off and (per `AGENTS.md`) a pre-registration receipt under
`docs/preregistration/`.

This file is maintained by the continuous audit loop
(`docs/audit/CONTINUOUS_AUDIT_LOG.md`). Pure correctness bugs (wrong math,
look-ahead, error handling) are fixed directly in-tree and logged in the audit
log; this file is only for changes that alter strategy *behavior/alpha*.

## Status legend

- 🟡 **Proposed** — written up, awaiting operator decision.
- 🔵 **Pre-registered** — receipt filed, ready to run as a labelled experiment.
- 🟢 **Accepted** — operator approved; landed with receipt.
- ⚪ **Rejected / parked** — with reason.

---

## Proposal queue

### 2026-06-18 — Fail closed on a persistent universe shrink  [🟡 Proposed]

**Sleeve:** continuous · **Type:** gate

**Observation.** `event_demo._resolve_cycle_universe` (continuous path) detects an
anomalously small universe (`universe.height < shrink_floor`, ~300 vs a healthy
~560–750), busts the cache, and retries once. But if the universe is STILL below
the floor after the retry it only `_logger.error(...)`s and **returns the shrunken
universe** — the only hard stop fires on a fully empty universe. The live decile
signal (`cross_sectional_decile`, denominator `pl.len().over("ts_ms")`) is then
computed over a non-representative subset, diverging live from the full-PIT
backtest manifest (the exact failure the floor was added to catch, 2026-05-24).
Confirmed + adversarially verified (audit-iter1 event-demo-2, MED).

**Proposed change.** Thread a `universe_degraded` flag out of
`_resolve_cycle_universe` (True when the shrink persists after retry) and add
`not universe_degraded` to the entry gate in `run_continuous_demo_cycle`, so NEW
entries are suppressed while exits / protective covers / risk management still run
on the held set. (The blunt alternative — raise — also halts exits during a venue
degradation, which is worse.)

**Expected effect & risk.** Live stops opening positions on a corrupted
cross-section instead of silently trading a biased subset; exits unaffected. Risk:
a noisy floor could suppress legitimate cycles — pick the floor/▸retry carefully
and log every suppression. Demo/paper only.

**Validation plan.** Unit test: a persistent shrink fires zero new entries but
still processes exits. Forward-watch the suppression-count telemetry. No alpha
claim; this is a faithfulness/safety guardrail.

**Decision.** (operator)

---

### 2026-06-18 — Provisional-trigger panel: gap-safe calendar windows  [🟡 Proposed]

**Sleeve:** long · **Type:** feature

**Observation.** `long_native._provisional_trigger_panel` builds the trailing-24h
FC features with POSITIONAL polars ops (`.shift(24)`, `rolling_*(24)`) over hourly
bars, which reach back 24 *existing rows* — across a kline gap that spans >24
wall-clock hours. The rest of the daily builder deliberately uses the gap-safe
`calendar_shift`/`calendar_roll` helpers (BAC-1/BAC-7) for exactly this reason, so
the provisional panel is non-causal/inconsistent on gapped symbols. Confirmed +
verified (audit-iter1 long-1, MED). **Latent:** `fc_provisional_entry` is False in
the deployed v11a profile, so no live run is affected today.

**Proposed change.** Replace the positional ops with `calendar_shift(...,
day_ms=MS_PER_HOUR)` / `calendar_roll(..., period_ms=MS_PER_HOUR)` anchored to the
hourly `ts_ms` grid, so the trailing-24h window is wall-clock anchored and nulls
(rather than stretching) across missing bars.

**Expected effect & risk.** Correctness only; no effect while provisional entries
are off. Per `AGENTS.md` this is a feature-window change → needs a pre-registration
receipt and is **numeric-equivalence gated**: on a contiguous-hourly symbol the new
expressions must be `np.allclose` to the old; on a deliberately gapped symbol they
must null instead of reaching across the gap. Add a regression test with an
injected hourly gap before merging.

**Decision.** (operator)

---

### 2026-06-18 — Long live sizing: replicate or assert the per-symbol gross cap  [🟡 Proposed]

**Sleeve:** long · **Type:** sizing

**Observation.** The backtest applies BOTH `max_position_weight` (inside the
vol-parity `min()`) AND a separate `max_per_symbol_weight` cap on final gross; the
live demo path applies only `max_position_weight`. Numerically inert in v11a
(`max_per_symbol_weight == max_position_weight == 0.30` and the cap never binds at
deployed params), but a real backtest↔live sizing divergence the moment an operator
sets them unequal. Confirmed + verified (audit-iter1 long-4, LOW).

**Proposed change.** Either (a) replicate the backtest's per-symbol gross cap in
the live sizing after the weekend/vol-target multipliers (mirroring
`long_native.py` ~2223), being careful that the live path applies `vol_target_scale`
at the book level vs the backtest folding it into `position_weight` before the cap;
or (b) add a fail-fast assert that `max_per_symbol_weight == max_position_weight`
for the long sleeve. (b) is the cheap safety net; (a) is the full fix.

**Expected effect & risk.** Restores the live==validated-backtest sizing invariant.
(a) touches a live order-sizing path → must be done carefully (cap placement vs the
book-level vol-target) or it introduces a different mismatch. Demo/paper only.

**Decision.** (operator)

---

### 2026-06-18 — Residualize rmom against a contemporaneous / shorter-horizon return  [⚪ Rejected / parked]

**Sleeve:** continuous · **Type:** feature

**Observation.** The residual-momentum gate residualizes against `fwd_ret_1d`
(`first_bar_close[D+2]/first_bar_close[D+1]−1`), which completes ≈(D+2) 01:00 and
forces `shift(3)`. Hypothesis (operator): residualize against a return that completes
sooner (contemporaneous `ret_1d`, or a shorter-horizon forward `fwd_intraday`) to get a
fresher signal. Researched in full: `docs/research/2026-06-18-residualization-target.md`
(harness `scripts/research_residualization_target.py` + `…_stage_b.py`; both full-PIT
venues; pipeline validated at >0.994 daily-rank-corr vs the deployed
`residual_momentum.parquet`).

**Finding.** Reject the literal change. **Contemporaneous `ret_1d` is ~half the |IC|**
of the live forward target at matched shift on both venues (over-orthogonalization: it
explains more *contemporaneous* variance but strips the persistent idiosyncratic
component the signal needs) — worse even at max freshness. The shorter-horizon forward
`fwd_intraday` *does* beat the live target by **+16–22% |IC|**, but only at `shift(2)` —
a ~20-minute, **zero operational-margin** position at the 00:20 refresh; at a deployable
~1-day margin (`shift(3)`) it merely ties `fwd_ret_1d`. The gain is pure freshness on the
same steep decay curve, i.e. exactly the operational-margin gap
`rmom-latency-falsification-2026-06-09` already flagged.

**Disposition.** Keep `fwd_ret_1d`. Parked, not dead: if the continuous book is ever
re-architected to consume a fast-decay daily signal with realistic intraday latency +
costs (the rmom-latency receipt's revival condition), `fwd_intraday` is the *legal* way
to harvest ~1 extra day of the freshness curve and should be re-tested **then, forward** —
not adopted now. `exploratory` label; gross-of-cost IC, no promotion claim.

**Decision.** (operator)

---

## Template

```
### YYYY-MM-DD — <short title>  [🟡 Proposed]

**Sleeve:** long | continuous | cross-sleeve
**Type:** entry | exit | sizing | hedge | feature | gate

**Observation.** What in the current logic motivates the change (cite file:line).

**Proposed change.** Precisely what would change, behaviorally.

**Expected effect & risk.** Directional hypothesis + what could go wrong.

**Validation plan.** How it would be tested (forward-demo arbiter, pre-reg id,
the three-tier decision rule). Remember: internal backtests are not promotion
evidence.

**Decision.** (operator)
```
