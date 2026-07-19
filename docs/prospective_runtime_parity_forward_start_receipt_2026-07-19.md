# Prospective Runtime-Parity Forward Start Receipt

Recorded 2026-07-19 after the create-only start receipt was published and
reopened, but before the registered 14:00 UTC boundary. This is an evidence
card, not an amendment, strategy result, deployment promotion, or real-money
authority.

## Final implementation and qualification

- Installed implementation commit:
  `9a2f20d85df2cf6211abd65e6c66249865026ad4`.
- Demo/paper-only operational authorization artifact SHA-256:
  `5485fa21401310332f1c543cb5945a0f8dcb15cc2cccead5a87020e64db58668`.
- The clean Linux gate passed Ruff, mypy over 108 sources, and 2,075 tests;
  two documented fixture-dependent tests were skipped.
- The final full-window comparator completed 29,449 cycles, 12,812 canonical
  account events, 911 accepted requests, all 235 registered lifecycle events,
  exact BTC-risk reconciliation, zero rejected strict risk-reduction batches,
  journal verification, and a flat terminal account. Monetary outcomes were
  not inspected.
- Comparator receipt file SHA-256:
  `f9ad5a6bfcc8948f742ae9bd877b8dda0e3f79d3908d96f274967445d6431e77`;
  payload SHA-256:
  `b6ee8d319d04f2837669d621f7d5430da06afbece9971b58d1cb0bfb677a46af`.
- Independent Linux verification receipt SHA-256:
  `bb6a8e755c2f07c7361dcb483fb46348b5806931a2027024c64659805dbb5a22`.
  It verified 87 files and 175,721,151 bytes under logical SHA-256
  `6babc66a5445d43f2559e2d6fc6838cceaf848c37cdd256398591928ed499699`.

The first comparator attempt at this commit completed structurally but failed
the final Windows atomic-directory rename. Its working directory and
termination receipt remain preserved; termination file SHA-256 is
`51750e881201d6905cb282f3eda3065c08113c240ad38f3c56dff5f5da51e052`.
It was not promoted. The independently rerun create-only output above is the
only comparator bound by the start receipt.

## Failed-closed pre-start repairs

The first start call created no receipt because the root-only collector used a
current-process-owner check for a correctly paper-owned route manifest.
`ba51bd63d3baf2c076504e6348ad3b1e97594c61` added an explicit expected-owner
UID for privileged read-only validation while preserving the current-owner
default for runtime callers.

The next call created no receipt because paper producer health still belonged
to the previous systemd generation. Logs showed that both paper producers were
computing cycles but their strict sandboxes denied the newly registered shared
paper target-capture root. `9a2f20d85df2cf6211abd65e6c66249865026ad4`
added only that already-authorized root to both paper producer write sets and
made the exact sets test-enforced. After activation, all four producers wrote
current-generation completed-cycle health; paper completion therefore also
proved successful shared-tape capture. No affected forward outcome, return,
TCA aggregate, or estimator result was read before either repair.

## Frozen boundary

- Receipt path:
  `reports/prospective-runtime-parity-execution-epoch-2026-07-18/forward/start/receipt.json`.
- Receipt file SHA-256:
  `db508862314972da310404814519bd701ffc18d2be51a3d39debddee1ef79376`.
- Self-hashed artifact SHA-256:
  `25441106b82adf95364d4e602d4b5912ecc0d2871b18778d8fe47684e8ddafbf`.
- Collected: 2026-07-19 13:09:37.924728 UTC.
- Start: 2026-07-19 14:00:00 UTC.
- Calibration end / validation start: 2026-09-02 14:00:00 UTC.
- Epoch end: 2026-10-17 14:00:00 UTC.

At collection, all six persistent services were on one current invocation with
zero restarts. The fully verified journals contained 15,524 demo and 90 paper
events. Demo and paper queues each contained zero pending, processing, and
failed requests. The shared tapes contained 123 demo and six paper rows; they
are recorded pre-boundary history and are not eligible forward observations.

The first 45 days are calibration-only. The second 45 days stay unopened until
the registered end, after which the frozen common-support TCA estimator and
decision rule may be applied once. No early look, optional stopping, clock
reset, return analysis, thesis qualification, mainnet use, or capital authority
follows from this receipt.
