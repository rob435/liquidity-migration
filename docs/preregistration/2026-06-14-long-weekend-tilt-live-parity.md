# Pre-registration: LONG weekend 1.5x tilt — live/backtest parity

**Date:** 2026-06-14
**Author:** rob435 (operator-approved 2026-06-14 via the audit flagged-items answers)
**Stage:** run-pending

## What's changing

Apply `strategy.weekend_size_mult` (1.5x) in the LONG v11a **LIVE demo** sizing
path so Sat/Sun entries are sized identically to the promotion backtest;
previously the tilt ran only in the backtest.

This is a live/backtest parity fix (the "same-code-illusion" class), not a new
parameter. The knob value itself (`weekend_size_mult=1.5`,
`long_native_event_demo.py:296`, originally registered under
`docs/preregistration/trade-atlas-2026-06-11.md`) is unchanged — only the path
that consumes it changes. No per-venue backtest numbers move; the backtest
already applied this tilt.

## What's changing — exact files / knobs touched

Staged (uncommitted) in the working tree, operator-approved 2026-06-14:

- `liquidity_migration/long_native_event_demo.py`
  - **L1118–1119** (new): in `_build_long_candidates` / the live demo entry
    sizing block, after the vol-parity weight is computed:
    ```python
    if strategy.weekend_size_mult != 1.0 and is_weekend_ms(now_ms):
        position_weight = position_weight * strategy.weekend_size_mult
    ```
    Keyed on `now_ms`, which is the actual live entry-ready timestamp
    (`entry_ready_ts_ms = now_ms`, written into the candidate at L1143).
  - **L10** (new import): `is_weekend_ms` added to the import from `._common`.
- No config / threshold knob is altered. `weekend_size_mult=1.5` was already set
  in `_v11a_long_native_config()` (L296) and consumed only by the backtest.

Shared helper (unchanged, the parity anchor):

- `liquidity_migration/_common.py:29` `is_weekend_ms(ts_ms)` — UTC Sat/Sun test.
  Both paths now call this single helper so live and backtest cannot drift.

Backtest application site (unchanged, the target this matches):

- `liquidity_migration/long_native.py:2157–2158` — `config.weekend_size_mult`
  applied keyed on `is_weekend_ms(int(entry_ts_ms))`, after the vol-target and
  drawdown scalars, before the per-symbol gross cap. (A second backtest entry
  path applies the same tilt at L1924–1925.) The live block mirrors this guard
  exactly (`!= 1.0` short-circuit + shared helper).

## Hypothesis

The promoted/backtested v11a profile sizes weekend-day (Sat/Sun UTC) FC entries
at 1.5x, but the live demo sleeve was applying only the vol-parity weight on
those days (1x). Roughly ~2/7 of calendar entry days are weekends, so the live
demo was running a structurally different sizing profile than the one validated
in the backtest — the forward-demo arbiter would therefore have been measuring
the wrong strategy on weekend entries (same-code-illusion). Applying the same
`weekend_size_mult` via the same `is_weekend_ms` helper, keyed on the real live
entry timestamp, makes the live demo sleeve size-equivalent to the promoted
profile on weekend entries.

## Predicted direction + magnitude

This is a parity/correctness fix, not a backtest-numbers change.

- Per-venue backtest Δ: **none**. The backtest already applied this tilt; the
  staged code touches only the live demo sizing path. Backtest equivalence on
  the per-venue roots is expected to be exact (NaN positions match, equity
  `np.allclose`), because no backtest code path changed.
- Live demo effect: on a **weekend** (UTC Sat/Sun) entry, the live notional
  `position_weight` is multiplied by 1.5x (vs the prior 1x); weekday entries are
  unchanged (the `is_weekend_ms` guard is false). No change to selection,
  pattern classification, retrace/deadline logic, or trade count — sizing only.
- Trade count Δ: 0 (sizing-only; no entry/exit gate is touched).
- Failure mode if hypothesis wrong: a live cycle takes a weekend entry and the
  recorded notional is still 1x (tilt not firing), OR a weekday entry is sized
  1.5x (helper mis-keyed / wrong timestamp), OR the forward-demo ledger fails to
  reconcile to the backtested v11a profile on weekend entries.

## Roots that will be touched

- [ ] bybit_full_pit (per-venue working dataset) — not touched; no backtest code path changed.
- [ ] binance_full_pit (per-venue working dataset) — not touched; no backtest code path changed.
- [x] forward demo/paper — affected once the LONG sleeve is re-enabled (latent until then).

Note on pre-reg scope: per `AGENTS.md` "Parameter pre-registration", a pre-reg is
mandatory for changes that touch the per-venue working datasets. This change does
not alter per-venue backtest numbers (it is a live-path parity fix), so it falls
under the execution/infra carve-out. It is filed as a full receipt anyway because
it changes what the forward-demo arbiter measures, and the forward demo/paper
ledger IS the Tier gate's evidence surface (STATE.md "Decision Rules"). The LONG
sleeve is currently `LONG_SLEEVE=off` in `deploy/sleeves.env`, so the change is
**latent** — it has no live effect until the sleeve is re-enabled (gated on Open
Operator Decision #4, LONG leverage/capital).

## Decision rule (a priori)

This is a binary parity gate, not a Tier promotion of new alpha (no new edge is
claimed; the existing v11a profile's promotion evidence is unchanged). Before
the LONG demo sleeve is re-enabled in `deploy/sleeves.env`:

> **Accept** the parity fix iff, in a live cycle that takes a Sat/Sun (UTC)
> entry, the recorded live notional equals the vol-parity weight × 1.5
> (`weekend_size_mult`), a weekday entry in the same window is sized ×1.0, AND
> the forward-demo ledger reconciles to the backtested v11a profile on those
> weekend entries (`scripts/reconcile.sh`, no weekend-entry sizing mismatch).
>
> **Reject** if any weekend entry still books at 1x, any weekday entry books at
> 1.5x, or the forward-demo ledger fails to reconcile to the backtested v11a
> profile on weekend entries.

The standing Tier gate is unchanged: re-enabling LONG for any forward-demo
evidence still requires the three-tier demo-arbiter gate in STATE.md
(MAR primary, Sharpe secondary; Tier 3 stays strict and currently unmet). This
receipt only certifies that, once enabled, the live sleeve measures the promoted
profile rather than a 1x-on-weekends variant.

## Run command

No backtest run is required to confirm parity (no backtest code path changed).
Confirmation is forward/live, performed before re-enabling the sleeve:

```bash
# 1. Static guard: ruff (no long command).
.venv/bin/python -m ruff check liquidity_migration/long_native_event_demo.py

# 2. After the LONG demo sleeve is re-enabled and a Sat/Sun-UTC entry fires,
#    reconcile the forward-demo ledger to the backtested v11a profile and
#    confirm the weekend entry's notional carries the 1.5x tilt.
bash scripts/reconcile.sh            # paper<->demo reconcile for the LONG v11a sleeve

# 3. Optional backtest-equivalence sanity (expected exact; no path changed):
#    promoted v11a profile via the equity-curve tool / promoted.py profile.
bash scripts/equity_curves.sh
```

## Post-run results

(fill in after the confirming live cycle; include the reconcile output, the
weekend-entry ledger row showing the 1.5x notional, and the commit SHA at which
the change landed)

## Verdict

(pending — to be recorded after the confirming live cycle and ledger reconcile,
before the LONG sleeve is re-enabled)
