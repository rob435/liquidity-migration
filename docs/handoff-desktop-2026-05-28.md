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

## The `event_demo` refactor (in flight, under audit)

A separate, legitimate refactor is splitting `event_demo.py` into
`event_demo_{data,entries,planning,exits,reports,daemon}.py` — currently under
audit, left as local WIP, **not** in this handoff commit.

- It does **not** affect your immediate task in practice: `volume_events.py`
  was split internally (filters/features/charts/validation siblings), but the
  `volume-events` CLI command + flags are unchanged (verified `--help`, exit 0).
  Do a quick `volume-events --help` on the desktop before the full run to confirm.
- It IS an upstream dependency for the later code-touch phases (R5 sizing, R6
  cost-model wiring, R12 sniper, C0 continuous engine) — build those on the
  post-refactor module layout once it merges. See the round2 doc "Codebase note
  — event_demo refactor in flight".
- Our research commit could not be pushed from the Mac: the repo's pre-push hook
  lints the whole working tree, and the refactor is mid-edit (failing ruff). The
  push lands once the refactor's audit settles the tree clean.
