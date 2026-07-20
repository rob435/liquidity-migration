# Freshness-gate-override prototype — forward rolling ledger (Lane 2)

Prototype: `../2026-07-20/prototype_freshness_gate_override.json`, registered
by commit `8d4ba374087f030ac77f1be403e3c5b47fc6bb20` (2026-07-19T23:37:40Z).
**First forward day: 2026-07-20.** Everything before that date is spent
context, never evidence.

## Ledger discipline

- `forward_ledger.csv` is append-only and hash-chained
  (`row_hash = sha256(prev_hash + canonical row)`, genesis
  `sha256("tj-forward-genesis")`). `tj_forward_scorer.py` verifies the whole
  chain before appending and cannot rewrite existing rows.
- A day is banked only when complete: entries' 24h holds realized inside the
  render window (any `data_end` exit stops the run before that day).
- Measurement (declared at registration): the gate-off-book realization of
  blocked entries with `hours_since_high_168h ≤ 1` at the entry bar close —
  both component-summed and single-counted nets are recorded. Capacity and
  sizing interactions of a real engine-level override are NOT modeled here;
  an engine implementation would be a separate, spec-frozen step.

## Scoring runbook (read-only against runtime; Windows box)

1. Refresh the bybit full-PIT research root through target day + 2.
2. Re-run the paired renders (baseline and `--research-disable-btc-gate`)
   with the T-A layout into a fresh directory (window start 2023-04-05 is
   fine — the scorer only reads new days).
3. `.venv\Scripts\python.exe scripts/research_v3/run_with_stub.py
   scripts/research_v3/tj_forward_scorer.py --render-root <render dir>`

`--preview` re-scores pre-commit days from the T-A renders into
`forward_preview.csv` (kept local, context only; mechanics validated
2026-07-20 — 1,158 days, totals reconcile with `tj_lead_robustness.json`).

No row in this ledger is an alpha, robustness, or promotion claim; promotion
remains a separate owner decision.
