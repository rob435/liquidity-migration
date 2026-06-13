# Composite Sizing / Regime Response - Current Stub

Operator-directed program from 2026-06-12.

## Closed

E1 capped composite size tilt ended at Stage-0 NO-GO. The failed receipt was
folded into [docs/research_summary.md](research_summary.md); git history is the
archive.

Decision: daily-granularity sizing conversion on the continuous book is closed.
Do not rerun E1 variants or rescue the tilt.

## Active

E2 BTC-trend regime response is the active receipt:
[docs/preregistration/2026-06-12-e2-regime-response-family.md](preregistration/2026-06-12-e2-regime-response-family.md).

Fixed variants:

- V0: current gate, `trend > 0`.
- V1: euphoria cap, `0 < trend <= +0.20`.
- V2: soft 3-state, `trend > +0.20` off, `0 < trend <= +0.20` full size,
  `trend <= 0` quarter-size top-composite-quintile entries.

No extra variants, threshold tuning, or rescue runs inside this program.
