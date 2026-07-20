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
  journal; any trip is executed as a five-line demotion note, a
  `deploy/sleeves.env` toggle (demo off, paper unchanged), and a recorded
  change point — the exact mirror of promotion.
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
