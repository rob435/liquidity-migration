# Desktop handoff — 2026-05-28 (R1 wide-funnel sweep)

**For the next Claude session on the 5950X desktop.** Read `STATE.md` +
`CLAUDE.md` / `AGENTS.md` first. This note is the specific next task plus the
context that won't be in your local memory (memory doesn't travel between machines).

## Immediate task — run the R1 wide-funnel sweep, then the verdict analyzer

```powershell
# Windows / PowerShell
$env:SWEEP_MAX_WORKERS=8; $env:POLARS_MAX_THREADS=4
.venv\Scripts\python.exe -u scripts\r1_filter_audit_sweep.py
```
```bash
# Linux / macOS
SWEEP_MAX_WORKERS=8 POLARS_MAX_THREADS=4 .venv/bin/python -u scripts/r1_filter_audit_sweep.py
```
Then the verdict + fragility diagnostics:
```bash
python scripts/r1_robustness.py --sweep-tag r1_filter_audit_max12_2026-05-28
```
Prereq: `~/SHARED_DATA/{bybit,binance}_full_pit` roots present (they were for Round 1).

## What this sweep is

7 cells × 2 venues, window 2023-04-01 → 2026-05-28, at **`max_active=12`**
(wide funnel, not the production 3). Goal: gather a LARGE trade dataset; the
per-trade ledgers carry every IC-feature value at entry, so afterward you can
filter the pool by feature thresholds (the R2/R9 feature-selection step). Cells:
`00_baseline` + `R1_drop_all_4` (lead) + 4 single-filter decompositions.

## Key decisions made this session (the "why")

- **Decision framework loosened to three-tier, demo-arbiter** (round2 doc
  "Decision framework"): Investigation → Demo-candidate (LOOSE: positive both
  venues + **pooled MAR Δ > +0.1**) → Real-money (STRICT: OOS + ≥30d demo +
  bootstrap p5 ≥ 0 + residual Sharpe + stress + capacity). Principle: permissive
  where being wrong is free, strict where it costs real money; the **forward
  demo is the arbiter** (it can't be overfit). Heavy stats live only at the
  real-money gate. This was an operator-instructed loosening, on principle, NOT
  to rescue a specific cell.
- **`max_active` 3 → 12**: the 3-slot cap was the dominant fragility — the lead
  candidate's edge rested on ~3 months. Wider funnel = more trades = more
  reliable edge read. Risk is bounded by gross exposure (each name ~8% of
  equity) + R4 factor caps, not by the count.
- `scripts/r1_robustness.py` now emits the Tier-2 verdict (pooled MAR Δ > +0.1,
  engine-DD MAR) + fragility (block-bootstrap p5, leave-one-month-out, thirds).

## Audit finding to keep in mind

`R1_drop_all_4` (the lead) Pareto-improved both venues in the Mac exploratory
peek (Bybit MAR Δ ≈ +1.29, Binance ≈ +1.03), BUT its edge was concentrated:
~70% of the Bybit lift came from 3 stress months; the Binance lift was basically
one month (2026-04, leave-one-out collapses +1.53x → +0.12x). At `max_active=12`
with ~4× the trades this should be less fragile — **check the bootstrap p5 +
leave-one-month-out in the verdict** before trusting it. (Those Mac exploratory
artifacts live in the Mac's `~/SHARED_DATA`, uncommitted — you'll regenerate at
12 slots; numbers won't be directly comparable to the 3-slot peek.)

## Collaboration preferences (not in your local memory)

- User owns/directs the project and is still building quant fundamentals —
  **explain with plain-language analogies.**
- **MAR-primary**, Sharpe secondary (pre-committed; don't flip mid-program).
- **Pre-push gate is MANDATORY**: `ruff check liquidity_migration tests` +
  `pytest -q` both green before every push. The user gets a CI-failure email on
  broken pushes.
- **Never set `REAL_MONEY=true`** — demo/paper only.
- Loosen at the cheap gate, keep the real-money gate strict; don't re-stack
  redundant fragility tests.

## NOT part of this handoff

There is an in-progress `event_demo` refactor on the Mac (extracting
`event_demo_data.py` out of `event_demo.py`) — unrelated to this research work,
left as local Mac WIP, deliberately **not** committed here.
