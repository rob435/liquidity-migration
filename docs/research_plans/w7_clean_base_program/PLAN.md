# W7 — Clean-Base Program (the next-month research direction)

**Opened:** 2026-06-16 (operator). **Successor to:** W5 (signal alpha) + W6 (bybit
orderflow/cost). **Status:** active.

This is a deliberate **reset + re-attack**. W5/W6 explored ~18 mechanisms and
converged on one robust edge (the BTC-vol regime *hedge*) while closing almost
everything else as NULL/venue-split/harmful. The operator's call (2026-06-16): the
deployed book is over-fit dressing on top of a few real ICs, and several of those
ICs were *closed prematurely because the conversion was crude* — the BTC-30d up/down
gate being the exemplar ("an extremely simplistic way of converting that IC into
returns"). W7 strips the book to a clean base and re-attacks the promising leads with
**better conversions and more effort**, under unchanged methodology discipline.

---

## 0. The organizing thesis (what W5/W6 actually taught us)

Two findings dominate and must shape every W7 stage:

1. **The edge is DIFFUSE.** The fade book profits when *broadly* deployed into
   dislocations. Every lever that *selects, shrinks, or concentrates* the entry set
   forwent that diffuse profit and failed to harvest: entry priority (Stage 1),
   path-shape priority+sizing (5/7b), liquidity selection (4/4d, venue-split),
   decel/funding entry (2), regime book-sizing (9), concurrency cap, exits both
   directions (3/3b). → **Do not build the better system out of stock-picking or
   position-sizing.**
2. **The loss TAIL is CORRELATED-CONCURRENT.** The book's worst days are *many
   concurrent correlated shorts squeezed together* (alt melt-ups, BTC-down regimes).
   The only lever that harvested was the one that **adds protection without shrinking
   the diffuse book** — the BTC-vol regime *hedge*. → **The conversion frontier for
   every real IC is RISK / TAIL / HEDGE allocation, not entry-selection or sizing.**

**W7 corollary:** we have several real ICs (regime, OI-squeeze, path-shape,
liquidity, funding/dispersion). Each was killed when converted to entry/sizing. The
W7 program's central bet is to **re-convert each IC into the tail/risk axis** — and to
do the few that genuinely belong on the entry axis with materially better encodings
than the crude binary cutoffs that closed them.

---

## 1. The clean research base (the new reference point)

Driver: `scripts/clean_base_2026-06-16.py`. Artifacts:
`~/SHARED_DATA/clean_base_2026-06-16/`. Run label: `exploratory_baseline`.

**STRIPPED (multiple-tested dressing — removed):**
- the 4-component ensemble + frozen weights `{.30/.20/.40/.10}`;
- the catalyst-threshold zoo (`turn3_pop3 / turn4_pop3 / turn4_pop5`);
- per-component age floors (`240/210`) and TPs (`10%/14%`);
- the **BTC-30d up/down entry gate** (the crude regime conversion);
- the daily vol-target / drawdown-half stack (`tv0.045/max4/ddh-0.04/w90`);
- inverse-vol sizing (`target_vol_per_name/clamp`);
- the 2f+regime **hedge** (kept as a *separable overlay*, not in the base).

**KEPT (W5-validated load-bearing + methodology — not over-fit):**
- core thesis: short the top `max_ret168` decile of **rmom-LOW** liquid names, fresh
  spell;
- 24h fixed hold, no TP (Stage 3/3b: exits both ways harmful, 24h near-optimal);
- crowding cap `max_fresh=2` (W5/W6: near-optimal; admitting more is worse — tail-
  correlated); single age floor 240 (fresh-listing squeeze control); liquidity floor;
- methodology gates: +1h causal fill, full PIT, modeled costs, funding.

**Base = one rule, ~6 knobs, flat 2%/name, gross 0.5, max_active 25, UNHEDGED, gate
off.** Baseline numbers (both venues) are filled in by the run → `§ Appendix A`.

> The base's knobs (`rmom_quantile`, `decile`, `age`, `liq`, `hold`, `crowd`) are
> **knobs to RE-DERIVE in Track A**, not frozen magic numbers. The point of the base
> is a clean, low-DOF surface to build on — not a new thing to over-tune.

---

## 2. Methodology spine (binding for every stage)

Unchanged from the repo standard; W7 adds two anti-false-rediscovery rules because we
are deliberately re-opening closed leads.

- **Both venues, full PIT, causal features, costs + funding, ledger/equity/splits.**
- **Plateau-not-spike:** any adopted knob must be robust across a neighborhood +
  chronological thirds, not a single favorable cut (the Stage-4 sniper trap).
- **Multi-seed / permutation nulls, never single-seed** (Stage 4d lesson); rank-IC +
  permutation, not tercile spread on heavy tails (Stage 7 lesson).
- **NEW — re-opens need a NEW mechanism, not a re-run.** A closed NULL may only be
  re-attacked with a *materially different conversion* + a fresh dated
  pre-registration. Re-mining the same window the same way is the multiple-testing
  trap; it is forbidden.
- **NEW — forward is the tiebreaker, and we now have it.** Both demo books are live
  (continuous gate-off plumbing test → revert to the clean base; long re-enabled),
  so fresh OOS is accruing. Any in-sample W7 result that survives must be carried to
  forward-watch; the strict **Tier-3 real-money gate is UNCHANGED**.
- Pre-registration receipts under `docs/preregistration/`; `EXPLORATORY` runs are
  never promotion evidence.

Decision tiers (from STATE.md): Tier-1 investigation → Tier-2 demo candidate
(pooled ΔMAR>+0.1, both-venue +, robust) / operator's looser "robust sub-+0.1"
forward-watch bar → Tier-3 real money (strict, unmet).

---

## 3. Tracks & stages (the month)

### Track A — Re-derive the base properly (Week 1)
The ensemble was a crutch averaging arbitrary knobs. Find the robust *single-rule*
operating point.
- **A0 — Reconstructability + candidate tape** on the clean base (mirror W5 Stage 0):
  per-cycle selected + rejected-eligible with engine reason, both venues, PIT gate.
  No alpha claim; the audit surface for everything after.
- **A1 — Core-knob plateaus:** sweep `rmom_quantile {.5,.4,.33,.25,.2}`,
  `decile {8,9}`, `age {0,120,240}`, `liq {250k,500k,1M}`, `hold {12,24,36,48}` —
  each a 1-D plateau check, cross-venue, on the clean base (no ensemble to hide
  fragility). Output: the defensible single operating point + its fragility map.
- **A2 — Feature honesty:** `max_ret168` (deployed) vs the 5-feature composite vs a
  re-derived parsimonious set — within-symbol rank-IC + permutation null. Is the pop-
  magnitude feature alone the right ranker, or is there a robust multi-feature lift?
- **A3 — Does a catalyst add over "none"?** Re-test turnover-surge + pop as a
  *continuous* score (not 3/4× cutoffs): does conditioning on catalyst intensity
  robustly beat the no-catalyst base, or was the catalyst zoo pure over-fit? Decision:
  keep `none` or adopt one continuous catalyst.

### Track B — Regime conversion, done right (Weeks 1–2) ← the operator's lead
The 30d gate was the crude conversion. The IC (BTC vol/trend regime predicts fade
outcomes) is real. Re-convert on the RISK axis, respecting that the book *profits* in
high-vol dislocations (so do NOT shrink it there — Stage 9 was harmful).
- **B1 — Continuous regime state (causal, both-venue, regularized):** build a regime
  index from BTC trailing vol (multi-horizon), trend, cross-section dispersion, and
  aggregate funding. NOT a binary gate; a smooth state. Validate the state's IC vs a
  hash-regime control before any conversion.
- **B2 — Regime → HEDGE intensity (deepen the one win):** generalize the Stage-8c
  BTC-vol hedge — multi-horizon vol, regime-conditioned instrument choice, and the
  binance dispersion hedge (Stage 10b, binance-robust) as a per-venue leg. Firm the
  binance 1.5×-cost thinness. Stack BTC-vol×dispersion (binance +0.368 seen).
- **B3 — Regime → HOLD/exit (untested combination):** hold longer in calm, shorter in
  turbulence — does regime-timed hold beat the fixed 24h *without* fighting the
  reversion thesis? (distinct from Stage 3/3b which moved hold unconditionally).
- **B4 — Regime → gross LEAN-IN (carefully):** the book profits in dislocations, so a
  regime that *adds* gross in (hedged) high-vol windows may harvest where size-DOWN
  failed. Must be paired with B2 hedge so the tail stays covered. The LEV control
  (+0.168) hint, done right.

### Track C — Orderflow / squeeze, re-converted to tail (Weeks 2–3)
OI-squeeze IC (`oi_chg_24h` +0.0665) real; sizing/hedge-regime/gross/admission all
NULL (correlated tail). Re-attack on the tail axis + unlock binance data.
- **C1 — Squeeze-breadth → hedge intensity** (distinct regime signal from BTC-vol):
  hedge MORE when market-wide OI-squeeze breadth is high. Tail protection, not entry.
- **C2 — Per-name squeeze → selective HEDGE, not sizing:** hedge the highest-squeeze-
  risk names (the ones that blow up the tail) rather than down-sizing them — keep the
  diffuse book, cap the tail.
- **C3 — Concurrent-squeeze circuit-breaker, validated:** the `entry_pause_*` knob
  exists but is untested for robustness; test a squeeze-breadth-triggered pause/hedge
  with a multi-seed null.
- **C4 — DATA: binance OI forward tape (E4)** — accrue both-venue orderflow (binance
  OI ~6wk, currently vacuous). Operator/host-gated.

### Track D — Path-shape & intrabar entry (Weeks 3–4, needs 5m)
Path-shape IC (`pre_24h_return` within-symbol +0.11) real; priority/sizing NULL.
Intrabar entry-price (B1) data-gated.
- **D1 — Path-shape → HOLD conditioning** (not entry/sizing): does the within-symbol
  pre-entry path predict fast vs slow reversion → condition the exit horizon?
- **D2 — Intrabar entry on 5m:** download bybit `klines_5m` (traded venue currently
  has only 1h locally). Re-test chase-limit / VWAP entry with the corrected fill
  convention (the W6 B1 bug is fixed). Closes the backtest↔live fill-price gap.
- **D3 — Maker/passive entry+exit economics** (the ≤8bp/trade ceiling): realizable
  only with real maker fills → calibrate from forward demo (Track E), not in-sample.

### Track E — Execution realism & forward calibration (continuous)
Closes the backtest≠live gap the granularity discussion exposed. Highest practical ROI.
- **E1 — Calibrate impact/cost from forward fills (the R4 debt):** the gate-off demo
  book is now placing real fills — fit the modeled impact coefficient to observed
  demo slippage. Makes every backtest number trustworthy.
- **E2 — Depth/capacity** (forward-only, maturing depth collector).
- **E3 — Reconcile clean-base backtest vs forward paper** continuously.

### Track F — Portfolio construction for a diffuse edge (Weeks 2–4)
The diffuse-edge/correlated-tail thesis is itself a research object.
- **F1 — Optimal gross given the hedge:** size leverage to the hedged risk, not raw
  (re-examine the LEV +0.168 with the hedge sized correctly — does it survive Tier-3
  tail/bootstrap?).
- **F2 — Correlation-aware risk budgeting** that hedges the concurrent-squeeze *factor*
  directly instead of capping breadth (the failed concurrency cap).
- **F3 — Fixed cross-sleeve tilt:** the dynamic LONG↔CONTINUOUS tilt was NULL, but a
  *fixed* ~30% long tilt diversifies (ρ≈0.03–0.07). Size the split properly; this is
  the open LONG-capital decision now that LONG is live.

### Track G — Forward-watch & promotion (all month)
- Revert the continuous gate to `uptrend` after the plumbing test, then run the
  **clean base** forward (demo + paper) as the reference book.
- Each robust in-sample increment → forward-watch; nothing promotes without the
  three-tier gate; Tier-3 unchanged; `REAL_MONEY` stays false.

---

## 4. Revisit ledger — prior result → why re-open → new approach

| Prior (W5/W6) | Verdict then | Why re-open | W7 re-attack |
|---|---|---|---|
| BTC-30d up/down gate | deployed, crude | binary cutoff of a real IC | B1 continuous regime state; B2/B4 risk-axis conversion |
| 4-strat ensemble + weights | deployed | over-fit averaging | A0–A3 single-rule re-derivation |
| BTC-vol regime hedge (8c) | the one win, thin on binance | deepen | B2 multi-horizon + dispersion per-venue |
| OI-squeeze IC (W6) | sizing/gross/admission NULL | correlated tail | C1/C2 squeeze→hedge, not entry/sizing |
| Path-shape IC (7b) | priority/sizing NULL | wrong axis | D1 path→hold conditioning |
| Liquidity/turnover (4d) | venue-split, bybit-noise | real on binance | folded into C2/F2 risk, not selection |
| Dispersion hedge (10b) | binance-robust only | per-venue leg | B2 BTC-vol×dispersion stack |
| Regime book-sizing (9) | harmful (shrinks winners) | wrong direction | B4 regime LEAN-IN (hedged) |
| Concurrency cap | harmful | shrinks diffuse book | F2/C3 hedge the factor instead |
| Intrabar entry B1 (W6) | NULL/data-gated | fill-convention bug since fixed | D2 on fresh bybit 5m |
| Maker/passive exit | forward-debt | needs real fills | D3/E1 forward calibration |

---

## 5. Schedule (indicative, 4 weeks)

- **Week 1:** A0–A3 (clean-base re-derivation) + B1 (regime state). Deliverable: the
  defensible single-rule base + a validated continuous regime index.
- **Week 2:** B2–B4 (regime → hedge/hold/lean-in) + C1 (squeeze→hedge) + F1.
- **Week 3:** C2–C4 (squeeze tail + binance OI data) + D1 (path→hold) + D2 (5m entry).
- **Week 4:** E1 forward calibration, F2/F3 portfolio construction, consolidation +
  forward-watch hand-off. Re-scope from results, don't grind.
- **Continuous:** E (forward calibration), G (forward-watch) run throughout.

## 6. Success criteria
A W7 "win" is a mechanism that, on the **clean base**, is: robust across its free
parameter + chronological thirds, positive on both venues (or operator's bybit-robust
/ binance-not-opposite bar), beats its negative + permutation control, survives cost
stress, keeps trades, AND is then confirmed on forward demo. The *strategic* win is a
**better IC→returns conversion on the risk/tail axis** than the crude gate/ensemble we
just stripped. Anything less stays `exploratory` and never touches the Tier-3 gate.

## Appendix A — Clean-base baseline numbers
From `scripts/clean_base_2026-06-16.py` (`~/SHARED_DATA/clean_base_2026-06-16/`),
config hashes bybit `20bf6f8cc868` / binance `9cfd12a97b1c`, 2023-04-01 → data end,
full-PIT, costed. Run label `exploratory_baseline`.

| Venue | Total return | MAR | Max DD | Sharpe | Trades | Funding |
|---|--:|--:|--:|--:|--:|--|
| bybit   | +21.1% | 0.58 | −11.6% | 1.16 | 1490 | partial |
| binance |  +5.3% | 0.11 | −15.7% | 0.30 | 1549 | partial |

Equity curves: `~/SHARED_DATA/clean_base_2026-06-16/<venue>/continuous_mtm_equity.png`.

**Baseline finding (sets W7 priorities).** The raw stripped fade is **positive on
both venues but modest** (MAR 0.58 / 0.11) with a fully-exposed tail (DD −11.6% /
−15.7%) — the diffuse-edge / correlated-tail picture, naked. Layer attribution on
bybit, same window/engine: **clean base 0.58 → + risk stack (hedge + vol-target +
ensemble + inverse-vol), gate off 1.29 → + BTC-30d regime gate 5.12.** So the
deployed book's risk-adjusted return comes *overwhelmingly from the risk/regime
overlays, not the entry signal*, and the (crude) regime conversion is the single
largest lever. This empirically confirms the §0 thesis and ranks the tracks:
**Track B (regime done right) is the highest-value attack, then the hedge/tail
tracks (B2/C), then portfolio construction (F);** entry-signal re-derivation (A) is
necessary hygiene but is NOT where the MAR lives. Caveat: both arms ran
`funding=partial` (the gate-off base reaches thinner-funding symbols/periods), so
costs are mildly understated — fold into Track E forward calibration.
