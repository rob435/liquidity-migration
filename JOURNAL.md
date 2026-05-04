# Journal

## 2026-05-04

- Implemented daily-close TWAP entry modeling:
  - signal at 22:00 UTC;
  - 60 equal 1m slices over `[22:00, 23:00)`;
  - average entry across fills;
  - disaster stop from first fill + 15m;
  - adaptive profit exits from final add + 15m;
  - partial weight if the disaster stop fires before full TWAP completion.
- Added ledger fields for TWAP accounting:
  `entry_twap_minutes`, `entry_fill_count`, `entry_fill_fraction`,
  `entry_complete_time`, `profit_protection_active_time`,
  `post_twap_hold_minutes`.
- Blocked forward paper entries when `entry_twap_minutes > 0` so the system
  cannot fake TWAP as one fill.
- Updated default config to the TWAP research contract.
- Ran the current-top-160 3-year TWAP benchmark:
  - 750 trades;
  - +16,896.41%;
  - Sharpe-like 10.63;
  - max drawdown -15.39%;
  - worst day -12.84%.
- Marked that result as biased until point-in-time archive validation.

## Earlier

- Removed the old live runtime and blended signal stack.
- Built isolated volume-alpha and daily-close-fade research paths.
- Added Bybit archive tooling for point-in-time universe work.
- Added demo-only Bybit plumbing, but demo execution is not alpha proof.
