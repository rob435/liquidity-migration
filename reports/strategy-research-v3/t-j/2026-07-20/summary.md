# T-J — Deployed-book conditioning search (V5 iteration, exploratory, Lane 1)

**Status: EXPLORATORY.** Owner-directed follow-up to V4 ("iterate and be
creative to find something that works"). The discovery surface moves from the
barebones ledger to the deployed-shape T-A render books, and every candidate
is judged with the controls the repository's 2026-06-19 sizing closure
demanded: era split, per-component consistency, seeded label-permutation
controls, tail arm, concentration/trim robustness. No alpha, robustness, or
promotion claim.

## Candidate 1 — exit-geometry hypothesis: KILLED by anatomy

Why is deep-negative funding the render books' best bucket but the barebones
book's worst? Not exits: the render books carry the **same exit shape**
(TP distance 12.0% exactly, 24h max hold, no stop — verified from the trade
files). The difference is selection: render deep-neg entries complete TP 41%
vs 26% barebones, and render max-hold deep-neg losers are gross-flat (+3.2%
over 210 trades gate-on) while the barebones max-hold deep-neg mass is −43.1%
net. The deployed admission/scoring picks reverting pumps; conditional exit
geometry has nothing to fix. (`tj_deepneg_anatomy.csv`)

## Candidate 2 — feature-conditional gate override: deep-neg REFUTED, at-high = lead

Blocked mass (gate-off entries absent from the same component's gate-on book;
1,742 trades, −2.2% net): the gate is right about stale entries
(blocked ∩ >24h = −9.5%) and about pos-funding (−7.2%), but blocks two
positive cells: at-high (+5.9%, both render eras) and deep-neg (+3.3%).
Barebones cross-check with the deployed gate's own trend value
(`tj_barebones_crosscheck.csv`):

- **deep-neg ∩ downtrend is negative in both barebones eras (−2.4 / −7.3) —
  the deep-neg override is refuted.**
- at-high ∩ downtrend is +1.4 early / −0.8 late on barebones — flat overall.
  The blocked-at-high value is concentrated entirely in the newest render era
  (2024-11 → 2026-07), which no other surface covers.

## Candidate 3 — freshness sizing tilt: FAILS on the deployed book (saturation)

Budget-neutral at-high upweight (M ∈ {1.25, 1.5}), 500 seeded permutation
controls per cell (`tj_tilt_controls.csv`):

| Book | ΔNet (M=1.25) | Era split | Perm percentile | Verdict |
|---|---:|---|---:|---|
| barebones | +3.26pp | +1.34 / +1.93 | 0.996 | passes everything |
| gate-off | +7.16pp | −0.22 / +7.38 | 0.994 | fails early era |
| **gate-on (deployed)** | +1.41pp | +0.16 / +1.25 | **0.750** | **inside permutation noise** |

Per-component gate-on: +0.60 / **−0.45** / +1.00 — not consistent. Cause:
at-high entries are already ~60% of the deployed book's notional — **the
deployed selection has saturated the freshness signal**; there is no room
left to tilt. (At M=1.5 the budget-neutral rest-weight degenerates to 0.18×.)

## The program-level answer

The deployed CONTINUOUS config sits at a local optimum with respect to every
coarse 1h-bar observable measured across V4+V5: its admission/scoring already
concentrates in exactly the cells (fresh highs, favorable funding) that
barebones diagnostics flag, and its complement cells are not net-negative.
Combined with the prior closures (flow, conviction sizing, intrabar timing,
stops, admission, vol-control, BTC-regime, adaptive exits), entry/exit/sizing
conditioning of this sleeve at 1h granularity is mined out on spent surfaces.
Fast closures are what a saturated surface looks like.

## The one surviving lead → forward-ledger prototype

**Blocked at-high entries, newest render era.** 617 trades 2024-11 → 2026-07:
+5.72% (component-summed gate-off units; +2.40% single-counted ≈ +1.5pp/yr
per book), 74.7% win rate, 158 symbols (top-3 = 52% of net), survives
dropping the top-5 trades (+4.58%), 13/18 months positive, consistent across
all three components (+1.66 / +2.38 / +1.68 late). **Known failure mode,
stated plainly:** 2025-03/04 lost −17.7pp before the favorable
2025-08 → 2026-07 run, and tail-day trades net −2.36pp over the window
(`tj_lead_by_month.csv`, `tj_lead_robustness.json`).

This does NOT meet the V4 double-verification bar (its support is one era of
one surface). Under the Progressive Evidence Model it is frozen as a Lane-2
prototype — `prototype_freshness_gate_override.json`: admit an
otherwise-gate-blocked entry iff `hours_since_high_168h ≤ 1` at the entry bar
close, everything else identical to deployed. **The commit of this file is
the registration; its evidence is exclusively the run of post-commit forward
days.** The spent-surface numbers above are context, not evidence. Promotion,
sizing, or any runtime change remains a separate owner decision.

## Limitations

- Discovered on already-rendered spent surfaces; second-generation selection
  (cells were chosen after seeing V4 cross-tabs) — selection risk is higher
  than V4's, which is why nothing here is graded on anything but forward days.
- Blocked-set identity matching assumes gate-off signals are a superset of
  gate-on on the same component/day (true by construction of the gate).
- Component-summed capital units double-count merged-signal overlap;
  single-counted magnitudes are reported beside them.
- No engine implementation exists yet for the override; building it (research
  flag or config) is a separate step that must not alter deployed behavior.

Artifacts: `tj_deepneg_anatomy.csv`, `tj_blocked_decomposition.csv`,
`tj_barebones_crosscheck.csv`, `tj_tilt_controls.csv`, `tj_lead_by_month.csv`,
`tj_lead_robustness.json`, `prototype_freshness_gate_override.json`, manifest.
