# Pre-registered sleeve kill criteria — 2026-07-20

Registered 2026-07-20, before any forward-epoch outcome read beyond the
operational facts already in `STATE.md`. This closes the promotion/demotion
asymmetry: promotion has always been a five-line note under
`docs/governance.md`; until today demotion had no pre-committed rule, which
left the decision to hindsight. These criteria are decided now, while we do
not know which way the evidence will go.

Scope: the two deployed strategy sleeves on the demo book. The hedge overlay
is a risk tool, not an alpha sleeve, and is governed by
`docs/hedge_refresh_policy.md` instead. Nothing here creates real-money
authority.

All quantities are measured from the canonical account journal projection
(realized + fees + funding, hedge attribution included for CONTINUOUS),
against the operational capital reference of 10,000 USDT. The forward epoch
is the registered runtime-parity epoch: 2026-07-19 14:00 UTC through
2026-10-17 14:00 UTC, first 45 days calibration-only.

## CONTINUOUS (`continuous_ensemble_v2`, demo components + hedge)

- **K1 — drawdown.** If peak-to-trough net P&L within the epoch is worse
  than **−500 USDT (−5% of capital reference)**, demote the demo CONTINUOUS
  components to paper-only at the next weekly check.
- **K2 — dead run.** At epoch day 90 (2026-10-17): if cumulative net P&L is
  ≤ 0 **and** the record contains ≥ 60 forward days **and** ≥ 30 completed
  component round trips, demote to paper-only. A smaller sample defers to
  K3.
- **K3 — insufficient sample.** If at epoch day 90 there are fewer than 30
  completed round trips, the record can neither promote nor demote; extend
  once by 90 days. If the extended record still has < 30 round trips, retire
  the demo components for capacity reasons: a signal too rare to validate is
  not a deployable signal at this design.

## LONG (`LongV11aDivWeekendVol`)

- **K1 — drawdown.** Peak-to-trough net P&L worse than **−400 USDT (−4% of
  capital reference)** within the epoch → demote to paper-only.
- **K2 — expectancy.** Once 40 completed round trips exist (whenever that
  occurs), if cumulative net expectancy per trade is ≤ 0, demote to
  paper-only.
- **K3 — staleness.** If epoch day 90 arrives with fewer than 15 completed
  round trips, extend once by 90 days. Fewer than 30 by day 180 retires the
  demo sleeve for capacity reasons, independent of sign.

## Mechanics annotation (both sleeves)

If over any trailing 14-day window more than 20% of accepted entry targets
are quantized by venue minimums to less than 80% of their intended notional,
the affected sleeve's forward record is labelled **mechanics-only** from the
start of that window until sizing is corrected, and mechanics-only days do
not count toward K2/K3 samples. (Registered together with the sizing floor
work of 2026-07-20; see `docs/forward_record_annotations.md`.)

## Process

- Checked weekly (operator or standing audit loop) from the canonical
  journal via `scripts/ops.sh kill-criteria` (read-only; exit 3 on any
  trip; implementation `liquidity_migration/sleeve_kill_criteria.py`).
  Any trip is executed as a five-line demotion note, a `deploy/sleeves.env`
  toggle (demo off, paper unchanged), and a recorded change point — the
  exact mirror of promotion.
- Bug fixes and behavior-preserving refactors never reset these clocks.
  A deliberate strategy-config replacement starts a new config record per
  the Progressive Evidence Model, but these criteria carry to the successor
  config unamended unless a new registration explicitly replaces them
  before its first forward day.
- These thresholds were chosen from the deployed sizing (CONTINUOUS: 25 ×
  2% components at 2x; LONG: 0.5× research sizing at 2x) so that K1
  represents roughly a 10% loss of maximum deployed gross — a level at
  which a fade/chase edge of the hypothesized size is very unlikely to be
  alive. They are deliberately blunt; a rule we might argue with later is
  the point.

## Amendment — 2026-07-27 (scale binding + LONG K2 gate)

Two silent disagreements between this registration and its executable form
(`liquidity_migration/sleeve_kill_criteria.py`) were found in the 2026-07-27
repo-wide audit (items H3 and H2) and are resolved here. Neither changes what
the criteria *mean*; both restore the meaning this document already states.

1. **K1 binds to the capital reference, not to the two absolute numbers.**
   The rules above state K1 as a percentage — "−500 USDT (−5% of capital
   reference)" and "−400 USDT (−4% of capital reference)" — and the closing
   annotation states the intent: K1 is "roughly a 10% loss of maximum deployed
   gross". The absolutes were the arithmetic of those percentages at the
   then-current 10,000 USDT reference. Commit `58c3432` scaled the operational
   profile 25× (`capital_reference_usdt: 250000`) and touched neither, which
   would have redefined K1 to ~0.4% of maximum deployed gross: at the deployed
   sizing one ordinary 1.5-ATR stop-out (~1,100 USDT) exceeds −400. From this
   amendment the executable form derives the limits from the **committed**
   operational profile's `capital_reference_usdt`:
   `continuous = −5%`, `long = −4%`. At the deployed 250,000 USDT reference
   those are −12,500 and −10,000 USDT. The percentages themselves are
   unchanged and are not re-openable by a sizing change; a future sizing change
   carries them automatically and needs no further amendment.
   The unattributed-P&L provisional flag (10% of the tightest limit) was
   already relative and scales with it.

2. **LONG K2 is not gated on epoch day 90.** The LONG K2 text is "once 40
   completed round trips exist (whenever that occurs)"; the day-90 /
   60-forward-day condition belongs to CONTINUOUS K2 only. The code required
   both for both sleeves, which could let a dead LONG run trade for up to two
   extra months. The executable form now gates day-90 per sleeve, and reports
   LONG K2 expectancy per trade explicitly.

Also implemented, without amendment (it was registered above and simply had no
executable form): LONG K3's second leg — fewer than 30 completed round trips by
day 180 retires the demo sleeve for capacity reasons, independent of sign.

Nothing here creates real-money authority, changes the epoch, or resets any
clock. Weekly checking is unchanged: `scripts/ops.sh kill-criteria`
(exit 3 on any trip).

## 2026-07-29 — CONTINUOUS retired from demo AND paper by owner override

Five lines, per this document's own Process section:

1. **What**: the CONTINUOUS sleeve (revision `active_single_fund0_tp12_sl35_v1`)
   is retired from BOTH the demo and paper fleets — `deploy/sleeves.env`
   `CONTINUOUS_SLEEVE=off`, `CONTINUOUS_PAPER_SLEEVE=off` — and replaced as
   the deployed non-LONG sleeve by the CARRY sleeve (registered config
   `lane2_carry_hold_v3`), which arrives in the immediately following change
   with its own kill-criteria registration.
2. **Why**: explicit owner instruction on 2026-07-28/29 ("depromote the
   continuous strat from demo and paper, and replace it with this one"),
   recorded as an operator override — not a K1/K2/K3 trip. No kill criterion
   fired; the sleeve's last honest same-window render was Sharpe 1.45 /
   +11.06% / max DD −1.84%.
3. **Scope**: stronger than any registered trip (a trip prescribes demo-off,
   paper-unchanged). Both toggles go off; existing CONTINUOUS/HEDGE exposure
   is flattened through the account owner before the rollout, per the
   standing flatness constraint. Unit files stay installed-but-disabled.
4. **Clock consequences**: this document's CONTINUOUS K1/K2/K3 clauses stop
   accruing at this change point — the frozen journal after retirement is a
   retirement artifact, NOT a K2/K3 dead-run trip at epoch day 90. LONG
   clauses are untouched and keep their clocks.
5. **Evidence status**: the CONTINUOUS forward record through this change
   point remains valid history under the Progressive Evidence Model;
   re-promotion of any CONTINUOUS revision would be a fresh five-line note
   plus change point, and would need a fresh universe/rule receipt pass.

Nothing here creates real-money authority. `REAL_MONEY=false` and mainnet
remain out of scope.
