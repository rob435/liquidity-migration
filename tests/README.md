# Test Suite Specification (`tests/`)

Structure, execution conventions, and quality gates for the Python test suite.

---

## 1. Directory Layout & Coverage Mapping

| Test Directory | Target Package / Scope | Invariants Verified |
| :--- | :--- | :--- |
| `tests/<package>/` | `liquidity_migration/<package>/` | Package unit tests matching module name (`test_<module>.py`). |
| `tests/scripts/` | `scripts/` & `deploy/` | Shell script syntax, CLI arguments, deployment logic, systemd templates. |
| `tests/repo/` | Repository-wide | Import order ranks, markdown link integrity, skill mirrors, dev tooling. |
| `tests/market_tape/`| `market_tape/` | Tape schema serialization, zstd segment compression, book reconstruction. |

---

## 2. Test Execution & Fencing Conventions

1. **Root Resolution**: Tests dynamically resolve repository root via `Path(__file__).resolve().parents[...]`.
2. **Git Fencing**: `tests/scripts/conftest.py` isolates git operations into temporary directories with host git discovery blocked.
3. **Running Tests**:
   ```bash
   # Run all Python tests
   .venv/bin/python -m pytest -q

   # Run specific package or module
   pytest -q tests/ops
   pytest -q tests/marketdata/test_bybit_market_data_boundary.py
   ```
