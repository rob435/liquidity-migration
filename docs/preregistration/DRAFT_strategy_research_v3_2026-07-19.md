# DRAFT — Strategy Research V3 thesis contracts (NOT REGISTERED)

Status: **DRAFT ONLY.** Nothing here is frozen, no hashes are pinned, no data
surface has been generated or read for these claims, and no runtime,
profile, sizing, or deployment change is proposed or authorized. Freezing
requires an explicit owner decision; until then this file has no evidentiary
or operational force. The active prospective runtime-parity epoch
(2026-07-19 14:00 → 2026-10-17 14:00 UTC) is untouched: the deployed profile,
including its BTC uptrend gate, stays frozen for the epoch's duration.

Fresh-cycle principles (V3):

- No inherited thresholds, venue rules, or metric presets from V1/V2. Every
  hurdle below is derived from the modeled cost of the exact trade shape at
  freeze time, not from repository folklore.
- Spent surfaces stay spent: the V2 discovery window `[2021-05-01, 2024-12-01)`
  and every inspected diagnostic remain hypothesis-generation material only.
- The `[2025-01-01, 2026-07-06)` holdout is spend-once. At most ONE frozen
  contract may name it. All others use genuinely new forward data
  (post-2026-07-06 accrual) or a prospectively named second-venue population
  (robustness only, never independence).
- Every contract carries an era-stability gate: the effect must hold in both
  halves of its evaluation window, or the thesis fails regardless of the
  pooled number. (Lesson: source_composite decayed +144 → +6 bps across eras.)

---

## T-A. Regime-gate ablation (owner input: remove gate for larger sample)

- **Proposition:** The BTC uptrend gate does not improve CONTINUOUS net
  economics per unit of tail risk; entries taken in the gated-off regime have
  net-after-cost economics no worse than gated entries, and the gate's joint
  drawdown protection is smaller than its opportunity cost.
- **Mechanism:** The gate is a trend filter but the documented shared tail is
  a vol/liquidation phenomenon, not a trend phenomenon; the gate may be
  paying a large sample/opportunity cost for protection it does not deliver.
- **Two registered arms (both must be declared before any outcome read):**
  1. *Economics arm:* equal-date net-after-cost difference, gated-off vs
     gated-on entry populations, against a hurdle equal to the modeled
     round-trip cost of the exact CONTINUOUS trade shape at freeze time.
  2. *Tail arm:* joint max-drawdown and worst-common-date contribution of a
     fixed-capital portfolio with and without the gate. Removal must not
     worsen the tail metric by more than a pre-declared bound even if the
     economics arm passes.
- **Population/venue:** Bybit full-PIT root; CONTINUOUS admitted labels under
  current admission rules (history + liquidity), both regimes.
- **Evaluation surface (to choose at freeze):** forward accrual
  `[2026-07-06, freeze-date)` as primary; holdout assignment only if the
  owner elects to spend it on this contract.
- **Decision rule:** both arms pass → register a post-epoch profile-change
  proposal through the normal stopped-install/authority flow. Either arm
  fails → gate stays; result recorded, surface marked spent.
- **Explicitly not claimed:** nothing about LONG; nothing about mainnet; no
  mid-epoch runtime change regardless of outcome.

## T-B. Funding-conditional entry (CONTINUOUS)

- **Proposition:** Skipping CONTINUOUS entries when the current (PIT-known)
  funding rate is below a frozen threshold improves net-after-cost economics
  per trade, because the −15.9% modeled funding drag concentrates in entries
  taken into already-crowded shorts.
- **Mechanism:** Short-side funding is the price of crowding; extreme negative
  funding at entry predicts continued negative carry over the holding window.
- **Comparator:** identical admission and exit rules, entry-skip rule as the
  only difference. Tested set: the exact threshold grid must be enumerated in
  the frozen contract; no post-hoc threshold selection.
- **Hurdle:** per-trade net improvement must exceed the modeled cost delta of
  the changed turnover profile; era-stability gate applies.
- **Surface:** forward accrual post-2026-07-06; does not require the exact
  deployed comparator, so it is NOT blocked on the RMOM provenance repair.

## T-C. Pump-deceleration entry timing (CONTINUOUS)

- **Proposition:** Requiring return deceleration (a frozen, PIT-computable
  momentum-of-momentum condition) before fade entry reduces adverse
  excursion (MAE) and stop-out frequency by more than the cost of missed
  entries.
- **Mechanism:** MAE −13.4% vs MFE +11.4% plus the live 2026-07-19 stop-out
  cluster indicate entries during accelerating pumps carry the deepest
  adverse paths; deceleration is observable causally at decision time.
- **Comparator/tested set:** the deceleration definition and its full
  parameter grid frozen before any outcome read; single primary metric:
  net-after-cost per trade including stop-out realizations; secondary: MAE
  distribution shift.
- **Surface:** forward accrual post-2026-07-06.

---

## Sequencing note (not a contract)

The RMOM provenance repair remains the prerequisite for any thesis that must
be evaluated against the exact deployed CONTINUOUS comparator (T-A's
profile-change decision ultimately wants it; T-B/T-C do not need it to
conclude at the population level). Execution/TCA hypotheses stay out of scope
until the registered 45-day calibration freezes its models.
