"""The study harness the research programs run on.

One dump of each point-in-time dataset (``dumps``), a daily symbol x day panel
built from it (``panel``), a numpy backtester for cross-sectional and time-series
rules on that panel (``backtest``), a per-trade exit overlay that replays a
recorded ledger under a different exit rule and scores it against a matched
random-exit placebo (``overlay``), the plateau checks a cell must survive after
beating its placebo (``plateau``), and the six-item evidence note from
``docs/research/governance.md`` (``evidence``). ``cli`` runs the dump and the
panel build from the shell. Everything here is Lane-1 tooling: it grades on
seen data unless the caller says otherwise in the note.
"""
