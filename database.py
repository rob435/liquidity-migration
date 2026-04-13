from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SignalRecord:
    timestamp: str
    stage: str
    signal_kind: str
    ticker: str
    momentum_z: float
    curvature: float
    hurst: float
    regime_score: int
    composite_score: float
    alerted: bool
    price: float
    rank: int
    persistence_hits: int
    dom_falling: bool
    dom_state: str
    dom_change_pct: float


class SignalDatabase:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute("PRAGMA busy_timeout=5000;")

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                timestamp TEXT NOT NULL,
                stage TEXT NOT NULL DEFAULT 'confirmed',
                signal_kind TEXT NOT NULL DEFAULT 'confirmed',
                ticker TEXT NOT NULL,
                momentum_z REAL NOT NULL,
                curvature REAL NOT NULL,
                hurst REAL NOT NULL,
                regime_score INTEGER NOT NULL,
                composite_score REAL NOT NULL,
                alerted INTEGER NOT NULL,
                price REAL NOT NULL DEFAULT 0,
                rank INTEGER NOT NULL DEFAULT 0,
                persistence_hits INTEGER NOT NULL DEFAULT 0,
                dom_falling INTEGER NOT NULL DEFAULT 0,
                dom_state TEXT NOT NULL DEFAULT 'neutral',
                dom_change_pct REAL NOT NULL DEFAULT 0
            )
            """
        )
        existing_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(signals)")
        }
        if "stage" not in existing_columns:
            self._conn.execute(
                "ALTER TABLE signals ADD COLUMN stage TEXT NOT NULL DEFAULT 'confirmed'"
            )
        if "signal_kind" not in existing_columns:
            self._conn.execute(
                "ALTER TABLE signals ADD COLUMN signal_kind TEXT NOT NULL DEFAULT 'confirmed'"
            )
        if "price" not in existing_columns:
            self._conn.execute("ALTER TABLE signals ADD COLUMN price REAL NOT NULL DEFAULT 0")
        if "rank" not in existing_columns:
            self._conn.execute("ALTER TABLE signals ADD COLUMN rank INTEGER NOT NULL DEFAULT 0")
        if "persistence_hits" not in existing_columns:
            self._conn.execute(
                "ALTER TABLE signals ADD COLUMN persistence_hits INTEGER NOT NULL DEFAULT 0"
            )
        if "dom_falling" not in existing_columns:
            self._conn.execute(
                "ALTER TABLE signals ADD COLUMN dom_falling INTEGER NOT NULL DEFAULT 0"
            )
        if "dom_state" not in existing_columns:
            self._conn.execute(
                "ALTER TABLE signals ADD COLUMN dom_state TEXT NOT NULL DEFAULT 'neutral'"
            )
        if "dom_change_pct" not in existing_columns:
            self._conn.execute(
                "ALTER TABLE signals ADD COLUMN dom_change_pct REAL NOT NULL DEFAULT 0"
            )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS summary_cycles (
                stage TEXT NOT NULL,
                cycle_time_ms INTEGER NOT NULL,
                PRIMARY KEY (stage, cycle_time_ms)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_order_id TEXT NOT NULL UNIQUE,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                signal_kind TEXT NOT NULL,
                requested_qty REAL NOT NULL,
                requested_notional_usd REAL NOT NULL,
                requested_price REAL NOT NULL,
                fill_price REAL,
                venue TEXT NOT NULL DEFAULT 'bybit',
                is_demo INTEGER NOT NULL DEFAULT 1,
                external_order_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                filled_at TEXT,
                notes TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL DEFAULT 'SHORT',
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT,
                entry_order_id INTEGER NOT NULL,
                exit_order_id INTEGER,
                entry_stage TEXT NOT NULL DEFAULT 'emerging',
                entry_signal_kind TEXT NOT NULL,
                quantity REAL NOT NULL,
                notional_usd REAL NOT NULL,
                entry_price REAL NOT NULL,
                take_profit_price REAL,
                stop_loss_price REAL,
                profit_protection_adjustments INTEGER NOT NULL DEFAULT 0,
                pending_exit_reason TEXT,
                exit_price REAL,
                regime_score_at_entry INTEGER,
                dom_state_at_entry TEXT,
                dom_change_pct_at_entry REAL,
                entry_rank INTEGER,
                entry_composite_score REAL,
                entry_momentum_z REAL,
                entry_curvature REAL,
                entry_hurst REAL,
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (entry_order_id) REFERENCES orders(id),
                FOREIGN KEY (exit_order_id) REFERENCES orders(id)
            )
            """
        )
        position_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(positions)")
        }
        if "side" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN side TEXT NOT NULL DEFAULT 'SHORT'"
            )
        if "entry_stage" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN entry_stage TEXT NOT NULL DEFAULT 'emerging'"
            )
        if "regime_score_at_entry" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN regime_score_at_entry INTEGER"
            )
        if "dom_state_at_entry" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN dom_state_at_entry TEXT"
            )
        if "dom_change_pct_at_entry" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN dom_change_pct_at_entry REAL"
            )
        if "entry_rank" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN entry_rank INTEGER"
            )
        if "entry_composite_score" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN entry_composite_score REAL"
            )
        if "entry_momentum_z" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN entry_momentum_z REAL"
            )
        if "entry_curvature" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN entry_curvature REAL"
            )
        if "entry_hurst" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN entry_hurst REAL"
            )
        if "take_profit_price" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN take_profit_price REAL"
            )
        if "stop_loss_price" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN stop_loss_price REAL"
            )
        if "profit_protection_adjustments" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN profit_protection_adjustments INTEGER NOT NULL DEFAULT 0"
            )
        if "pending_exit_reason" not in position_columns:
            self._conn.execute(
                "ALTER TABLE positions ADD COLUMN pending_exit_reason TEXT"
            )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_analytics (
                position_id INTEGER PRIMARY KEY,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_stage TEXT NOT NULL,
                entry_signal_kind TEXT NOT NULL,
                exit_stage TEXT NOT NULL,
                exit_signal_kind TEXT NOT NULL,
                exit_event TEXT NOT NULL,
                exit_reason TEXT NOT NULL DEFAULT '',
                opened_at TEXT NOT NULL,
                closed_at TEXT NOT NULL,
                holding_minutes REAL NOT NULL DEFAULT 0,
                bars_held INTEGER NOT NULL DEFAULT 0,
                quantity REAL NOT NULL,
                notional_usd REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                realized_pnl_pct REAL NOT NULL,
                realized_pnl_usd REAL NOT NULL,
                mfe_pct REAL NOT NULL DEFAULT 0,
                mae_pct REAL NOT NULL DEFAULT 0,
                peak_favorable_price REAL,
                peak_adverse_price REAL,
                intratrade_volatility REAL,
                regime_score_at_entry INTEGER,
                dom_state_at_entry TEXT,
                dom_change_pct_at_entry REAL,
                entry_rank INTEGER,
                entry_composite_score REAL,
                entry_momentum_z REAL,
                entry_curvature REAL,
                entry_hurst REAL,
                exit_rank INTEGER,
                exit_composite_score REAL,
                exit_momentum_z REAL,
                exit_curvature REAL,
                exit_hurst REAL,
                take_profit_price_at_exit REAL,
                stop_loss_price_at_exit REAL,
                profit_protection_adjustments INTEGER NOT NULL DEFAULT 0,
                minutes_to_first_profit_50bps REAL,
                minutes_to_first_profit_100bps REAL,
                notes TEXT NOT NULL DEFAULT '',
                post_exit_bars_target INTEGER NOT NULL DEFAULT 0,
                post_exit_bars_observed INTEGER NOT NULL DEFAULT 0,
                post_exit_completed INTEGER NOT NULL DEFAULT 0,
                post_exit_tracking_started_at TEXT,
                post_exit_tracking_completed_at TEXT,
                post_exit_last_price REAL,
                post_exit_best_price REAL,
                post_exit_worst_price REAL,
                post_exit_favorable_excursion_pct REAL NOT NULL DEFAULT 0,
                post_exit_adverse_excursion_pct REAL NOT NULL DEFAULT 0,
                post_exit_volatility REAL,
                FOREIGN KEY (position_id) REFERENCES positions(id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS position_marks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                phase TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                stage TEXT NOT NULL,
                price REAL NOT NULL,
                pnl_pct REAL NOT NULL,
                favorable_excursion_pct REAL NOT NULL DEFAULT 0,
                adverse_excursion_pct REAL NOT NULL DEFAULT 0,
                rolling_volatility REAL,
                regime_score INTEGER,
                dom_state TEXT,
                dom_change_pct REAL,
                bars_since_entry INTEGER NOT NULL DEFAULT 0,
                bars_since_exit INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (position_id) REFERENCES positions(id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                stage TEXT NOT NULL,
                event TEXT NOT NULL,
                wallet_equity_usd REAL,
                available_balance_usd REAL,
                open_positions INTEGER NOT NULL,
                gross_notional_usd REAL NOT NULL DEFAULT 0,
                remaining_slots INTEGER,
                balance_position_capacity INTEGER,
                risk_budget_usd REAL,
                target_notional_usd REAL,
                daily_stop_loss_count INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS run_manifests (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                git_commit TEXT NOT NULL,
                config_fingerprint TEXT NOT NULL,
                execution_enabled INTEGER NOT NULL DEFAULT 0,
                execution_submit_orders INTEGER NOT NULL DEFAULT 0,
                demo_mode INTEGER NOT NULL DEFAULT 1,
                telegram_enabled INTEGER NOT NULL DEFAULT 0,
                operator_pause_new_entries INTEGER NOT NULL DEFAULT 0,
                universe_size INTEGER NOT NULL DEFAULT 0,
                momentum_reference_mode TEXT NOT NULL DEFAULT 'absolute',
                cluster_assignment_mode TEXT NOT NULL DEFAULT 'manual',
                notes TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_health_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                bootstraps INTEGER NOT NULL DEFAULT 0,
                macro_refreshes INTEGER NOT NULL DEFAULT 0,
                processed_cycles INTEGER NOT NULL DEFAULT 0,
                processed_confirmed_cycles INTEGER NOT NULL DEFAULT 0,
                processed_emerging_cycles INTEGER NOT NULL DEFAULT 0,
                websocket_sessions INTEGER NOT NULL DEFAULT 0,
                websocket_failures INTEGER NOT NULL DEFAULT 0,
                queue_drops INTEGER NOT NULL DEFAULT 0,
                confirmed_queue_drops INTEGER NOT NULL DEFAULT 0,
                emerging_queue_drops INTEGER NOT NULL DEFAULT 0,
                confirmed_queue_size INTEGER NOT NULL DEFAULT 0,
                emerging_queue_size INTEGER NOT NULL DEFAULT 0,
                open_positions INTEGER NOT NULL DEFAULT 0,
                daily_stop_loss_count INTEGER NOT NULL DEFAULT 0,
                last_bootstrap_at TEXT,
                last_macro_refresh_at TEXT,
                last_provisional_event_at TEXT,
                last_confirmed_event_at TEXT,
                last_processed_confirmed_at TEXT,
                last_processed_emerging_at TEXT,
                last_drift_check_at TEXT,
                drift_status TEXT NOT NULL DEFAULT 'not_run',
                operator_pause_new_entries INTEGER NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (run_id) REFERENCES run_manifests(run_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                created_at TEXT NOT NULL,
                severity TEXT NOT NULL,
                component TEXT NOT NULL,
                event_type TEXT NOT NULL,
                stage TEXT,
                ticker TEXT,
                detail TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES run_manifests(run_id)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reconciliation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                window_date TEXT NOT NULL,
                actual_source TEXT NOT NULL,
                backtest_source TEXT NOT NULL,
                actual_entries INTEGER NOT NULL DEFAULT 0,
                backtest_entries INTEGER NOT NULL DEFAULT 0,
                matched_entries INTEGER NOT NULL DEFAULT 0,
                entry_precision REAL NOT NULL DEFAULT 0,
                entry_recall REAL NOT NULL DEFAULT 0,
                actual_exits INTEGER NOT NULL DEFAULT 0,
                backtest_exits INTEGER NOT NULL DEFAULT 0,
                matched_exits INTEGER NOT NULL DEFAULT 0,
                exit_precision REAL NOT NULL DEFAULT 0,
                exit_recall REAL NOT NULL DEFAULT 0,
                matched_exit_reason_count INTEGER NOT NULL DEFAULT 0,
                mismatched_exit_reason_count INTEGER NOT NULL DEFAULT 0,
                actual_unique_tickers INTEGER NOT NULL DEFAULT 0,
                backtest_unique_tickers INTEGER NOT NULL DEFAULT 0,
                matched_unique_tickers INTEGER NOT NULL DEFAULT 0,
                ticker_precision REAL NOT NULL DEFAULT 0,
                ticker_recall REAL NOT NULL DEFAULT 0,
                export_dir TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_signals_ticker_timestamp
            ON signals (ticker, timestamp)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orders_ticker_status
            ON orders (ticker, status, lifecycle)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_positions_ticker_status
            ON positions (ticker, status)
            """
        )
        self._conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_positions_open_ticker
            ON positions (ticker)
            WHERE status = 'open'
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_trade_analytics_closed_at
            ON trade_analytics (closed_at, exit_event)
            """
        )
        trade_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(trade_analytics)")
        }
        if "notes" not in trade_columns:
            self._conn.execute(
                "ALTER TABLE trade_analytics ADD COLUMN notes TEXT NOT NULL DEFAULT ''"
            )
        if "exit_rank" not in trade_columns:
            self._conn.execute("ALTER TABLE trade_analytics ADD COLUMN exit_rank INTEGER")
        if "exit_composite_score" not in trade_columns:
            self._conn.execute("ALTER TABLE trade_analytics ADD COLUMN exit_composite_score REAL")
        if "exit_momentum_z" not in trade_columns:
            self._conn.execute("ALTER TABLE trade_analytics ADD COLUMN exit_momentum_z REAL")
        if "exit_curvature" not in trade_columns:
            self._conn.execute("ALTER TABLE trade_analytics ADD COLUMN exit_curvature REAL")
        if "exit_hurst" not in trade_columns:
            self._conn.execute("ALTER TABLE trade_analytics ADD COLUMN exit_hurst REAL")
        if "take_profit_price_at_exit" not in trade_columns:
            self._conn.execute(
                "ALTER TABLE trade_analytics ADD COLUMN take_profit_price_at_exit REAL"
            )
        if "stop_loss_price_at_exit" not in trade_columns:
            self._conn.execute(
                "ALTER TABLE trade_analytics ADD COLUMN stop_loss_price_at_exit REAL"
            )
        if "profit_protection_adjustments" not in trade_columns:
            self._conn.execute(
                "ALTER TABLE trade_analytics ADD COLUMN profit_protection_adjustments INTEGER NOT NULL DEFAULT 0"
            )
        if "minutes_to_first_profit_50bps" not in trade_columns:
            self._conn.execute(
                "ALTER TABLE trade_analytics ADD COLUMN minutes_to_first_profit_50bps REAL"
            )
        if "minutes_to_first_profit_100bps" not in trade_columns:
            self._conn.execute(
                "ALTER TABLE trade_analytics ADD COLUMN minutes_to_first_profit_100bps REAL"
            )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_position_marks_position_phase
            ON position_marks (position_id, phase, timestamp)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_timestamp
            ON portfolio_snapshots (timestamp, stage)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_runtime_health_run_time
            ON runtime_health_snapshots (run_id, recorded_at)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_runtime_events_run_time
            ON runtime_events (run_id, created_at)
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reconciliation_runs_window_time
            ON reconciliation_runs (window_date, created_at)
            """
        )
        self._conn.commit()

    async def log_signal(self, record: SignalRecord) -> None:
        await asyncio.to_thread(self._log_signal_sync, record)

    async def log_signals(self, records: list[SignalRecord]) -> None:
        if not records:
            return
        await asyncio.to_thread(self._log_signals_sync, records)

    def _log_signal_sync(self, record: SignalRecord) -> None:
        self._log_signals_sync([record])

    def _log_signals_sync(self, records: list[SignalRecord]) -> None:
        self._conn.execute(
            "BEGIN"
        )
        self._conn.executemany(
            """
            INSERT INTO signals (
                timestamp,
                stage,
                signal_kind,
                ticker,
                momentum_z,
                curvature,
                hurst,
                regime_score,
                composite_score,
                alerted,
                price,
                rank,
                persistence_hits,
                dom_falling,
                dom_state,
                dom_change_pct
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.timestamp,
                    record.stage,
                    record.signal_kind,
                    record.ticker,
                    record.momentum_z,
                    record.curvature,
                    record.hurst,
                    record.regime_score,
                    record.composite_score,
                    int(record.alerted),
                    record.price,
                    record.rank,
                    record.persistence_hits,
                    int(record.dom_falling),
                    record.dom_state,
                    record.dom_change_pct,
                )
                for record in records
            ],
        )
        self._conn.commit()

    async def record_summary_cycle(self, stage: str, cycle_time_ms: int) -> bool:
        return await asyncio.to_thread(self._record_summary_cycle_sync, stage, cycle_time_ms)

    def _record_summary_cycle_sync(self, stage: str, cycle_time_ms: int) -> bool:
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO summary_cycles (stage, cycle_time_ms)
            VALUES (?, ?)
            """,
            (stage, cycle_time_ms),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    async def create_order(
        self,
        *,
        client_order_id: str,
        ticker: str,
        side: str,
        order_type: str,
        lifecycle: str,
        status: str,
        stage: str,
        signal_kind: str,
        requested_qty: float,
        requested_notional_usd: float,
        requested_price: float,
        venue: str,
        is_demo: bool,
        created_at: str,
        updated_at: str,
        fill_price: float | None = None,
        external_order_id: str | None = None,
        filled_at: str | None = None,
        notes: str = "",
    ) -> int:
        return await asyncio.to_thread(
            self._create_order_sync,
            client_order_id,
            ticker,
            side,
            order_type,
            lifecycle,
            status,
            stage,
            signal_kind,
            requested_qty,
            requested_notional_usd,
            requested_price,
            venue,
            is_demo,
            created_at,
            updated_at,
            fill_price,
            external_order_id,
            filled_at,
            notes,
        )

    def _create_order_sync(
        self,
        client_order_id: str,
        ticker: str,
        side: str,
        order_type: str,
        lifecycle: str,
        status: str,
        stage: str,
        signal_kind: str,
        requested_qty: float,
        requested_notional_usd: float,
        requested_price: float,
        venue: str,
        is_demo: bool,
        created_at: str,
        updated_at: str,
        fill_price: float | None,
        external_order_id: str | None,
        filled_at: str | None,
        notes: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO orders (
                client_order_id,
                ticker,
                side,
                order_type,
                lifecycle,
                status,
                stage,
                signal_kind,
                requested_qty,
                requested_notional_usd,
                requested_price,
                fill_price,
                venue,
                is_demo,
                external_order_id,
                created_at,
                updated_at,
                filled_at,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                client_order_id,
                ticker,
                side,
                order_type,
                lifecycle,
                status,
                stage,
                signal_kind,
                requested_qty,
                requested_notional_usd,
                requested_price,
                fill_price,
                venue,
                int(is_demo),
                external_order_id,
                created_at,
                updated_at,
                filled_at,
                notes,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    async def get_active_order(self, ticker: str, lifecycle: str) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._get_active_order_sync, ticker, lifecycle)

    def _get_active_order_sync(self, ticker: str, lifecycle: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT *
            FROM orders
            WHERE ticker = ?
              AND lifecycle = ?
              AND status IN ('created', 'submitted')
            ORDER BY id DESC
            LIMIT 1
            """,
            (ticker, lifecycle),
        ).fetchone()

    async def open_position(
        self,
        *,
        ticker: str,
        side: str,
        opened_at: str,
        updated_at: str,
        entry_order_id: int,
        entry_stage: str,
        entry_signal_kind: str,
        quantity: float,
        notional_usd: float,
        entry_price: float,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
        profit_protection_adjustments: int = 0,
        regime_score_at_entry: int | None = None,
        dom_state_at_entry: str | None = None,
        dom_change_pct_at_entry: float | None = None,
        entry_rank: int | None = None,
        entry_composite_score: float | None = None,
        entry_momentum_z: float | None = None,
        entry_curvature: float | None = None,
        entry_hurst: float | None = None,
        notes: str = "",
    ) -> int:
        return await asyncio.to_thread(
            self._open_position_sync,
            ticker,
            side,
            opened_at,
            updated_at,
            entry_order_id,
            entry_stage,
            entry_signal_kind,
            quantity,
            notional_usd,
            entry_price,
            take_profit_price,
            stop_loss_price,
            profit_protection_adjustments,
            regime_score_at_entry,
            dom_state_at_entry,
            dom_change_pct_at_entry,
            entry_rank,
            entry_composite_score,
            entry_momentum_z,
            entry_curvature,
            entry_hurst,
            notes,
        )

    def _open_position_sync(
        self,
        ticker: str,
        side: str,
        opened_at: str,
        updated_at: str,
        entry_order_id: int,
        entry_stage: str,
        entry_signal_kind: str,
        quantity: float,
        notional_usd: float,
        entry_price: float,
        take_profit_price: float | None,
        stop_loss_price: float | None,
        profit_protection_adjustments: int,
        regime_score_at_entry: int | None,
        dom_state_at_entry: str | None,
        dom_change_pct_at_entry: float | None,
        entry_rank: int | None,
        entry_composite_score: float | None,
        entry_momentum_z: float | None,
        entry_curvature: float | None,
        entry_hurst: float | None,
        notes: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO positions (
                ticker,
                side,
                status,
                opened_at,
                updated_at,
                entry_order_id,
                entry_stage,
                entry_signal_kind,
                quantity,
                notional_usd,
                entry_price,
                take_profit_price,
                stop_loss_price,
                profit_protection_adjustments,
                regime_score_at_entry,
                dom_state_at_entry,
                dom_change_pct_at_entry,
                entry_rank,
                entry_composite_score,
                entry_momentum_z,
                entry_curvature,
                entry_hurst,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                side,
                "open",
                opened_at,
                updated_at,
                entry_order_id,
                entry_stage,
                entry_signal_kind,
                quantity,
                notional_usd,
                entry_price,
                take_profit_price,
                stop_loss_price,
                profit_protection_adjustments,
                regime_score_at_entry,
                dom_state_at_entry,
                dom_change_pct_at_entry,
                entry_rank,
                entry_composite_score,
                entry_momentum_z,
                entry_curvature,
                entry_hurst,
                notes,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    async def update_position_protection(
        self,
        position_id: int,
        *,
        take_profit_price: float,
        stop_loss_price: float,
        profit_protection_adjustments: int,
        updated_at: str,
        notes: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._update_position_protection_sync,
            position_id,
            take_profit_price,
            stop_loss_price,
            profit_protection_adjustments,
            updated_at,
            notes,
        )

    def _update_position_protection_sync(
        self,
        position_id: int,
        take_profit_price: float,
        stop_loss_price: float,
        profit_protection_adjustments: int,
        updated_at: str,
        notes: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE positions
            SET take_profit_price = ?,
                stop_loss_price = ?,
                profit_protection_adjustments = ?,
                updated_at = ?,
                notes = CASE
                    WHEN ? = '' THEN notes
                    WHEN notes = '' THEN ?
                    ELSE notes || ' | ' || ?
                END
            WHERE id = ?
            """,
            (
                take_profit_price,
                stop_loss_price,
                profit_protection_adjustments,
                updated_at,
                notes,
                notes,
                notes,
                position_id,
            ),
        )
        self._conn.commit()

    async def get_open_position(self, ticker: str) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._get_open_position_sync, ticker)

    def _get_open_position_sync(self, ticker: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT *
            FROM positions
            WHERE ticker = ?
              AND status = 'open'
            ORDER BY id DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()

    async def list_open_positions(self) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._list_open_positions_sync)

    def _list_open_positions_sync(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT *
            FROM positions
            WHERE status = 'open'
            ORDER BY id ASC
            """
        ).fetchall()

    async def close_position(
        self,
        position_id: int,
        exit_order_id: int,
        exit_price: float,
        closed_at: str,
        updated_at: str,
        notes: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._close_position_sync,
            position_id,
            exit_order_id,
            exit_price,
            closed_at,
            updated_at,
            notes,
        )

    def _close_position_sync(
        self,
        position_id: int,
        exit_order_id: int,
        exit_price: float,
        closed_at: str,
        updated_at: str,
        notes: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE positions
            SET status = 'closed',
                exit_order_id = ?,
                exit_price = ?,
                closed_at = ?,
                updated_at = ?,
                notes = CASE
                    WHEN ? = '' THEN notes
                    WHEN notes = '' THEN ?
                    ELSE notes || ' | ' || ?
                END
            WHERE id = ?
            """,
            (
                exit_order_id,
                exit_price,
                closed_at,
                updated_at,
                notes,
                notes,
                notes,
                position_id,
            ),
        )
        self._conn.commit()

    async def log_position_mark(
        self,
        *,
        position_id: int,
        ticker: str,
        side: str,
        phase: str,
        timestamp: str,
        stage: str,
        price: float,
        pnl_pct: float,
        favorable_excursion_pct: float,
        adverse_excursion_pct: float,
        rolling_volatility: float | None,
        regime_score: int | None,
        dom_state: str | None,
        dom_change_pct: float | None,
        bars_since_entry: int = 0,
        bars_since_exit: int = 0,
    ) -> int:
        return await asyncio.to_thread(
            self._log_position_mark_sync,
            position_id,
            ticker,
            side,
            phase,
            timestamp,
            stage,
            price,
            pnl_pct,
            favorable_excursion_pct,
            adverse_excursion_pct,
            rolling_volatility,
            regime_score,
            dom_state,
            dom_change_pct,
            bars_since_entry,
            bars_since_exit,
        )

    def _log_position_mark_sync(
        self,
        position_id: int,
        ticker: str,
        side: str,
        phase: str,
        timestamp: str,
        stage: str,
        price: float,
        pnl_pct: float,
        favorable_excursion_pct: float,
        adverse_excursion_pct: float,
        rolling_volatility: float | None,
        regime_score: int | None,
        dom_state: str | None,
        dom_change_pct: float | None,
        bars_since_entry: int,
        bars_since_exit: int,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO position_marks (
                position_id,
                ticker,
                side,
                phase,
                timestamp,
                stage,
                price,
                pnl_pct,
                favorable_excursion_pct,
                adverse_excursion_pct,
                rolling_volatility,
                regime_score,
                dom_state,
                dom_change_pct,
                bars_since_entry,
                bars_since_exit
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                position_id,
                ticker,
                side,
                phase,
                timestamp,
                stage,
                price,
                pnl_pct,
                favorable_excursion_pct,
                adverse_excursion_pct,
                rolling_volatility,
                regime_score,
                dom_state,
                dom_change_pct,
                bars_since_entry,
                bars_since_exit,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    async def list_position_marks(
        self,
        position_id: int,
        phase: str | None = None,
    ) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._list_position_marks_sync, position_id, phase)

    def _list_position_marks_sync(
        self,
        position_id: int,
        phase: str | None,
    ) -> list[sqlite3.Row]:
        if phase is None:
            return self._conn.execute(
                """
                SELECT *
                FROM position_marks
                WHERE position_id = ?
                ORDER BY id ASC
                """,
                (position_id,),
            ).fetchall()
        return self._conn.execute(
            """
            SELECT *
            FROM position_marks
            WHERE position_id = ?
              AND phase = ?
            ORDER BY id ASC
            """,
            (position_id, phase),
        ).fetchall()

    async def record_trade_analytics(self, **kwargs) -> None:
        await asyncio.to_thread(self._record_trade_analytics_sync, kwargs)

    def _record_trade_analytics_sync(self, data: dict) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO trade_analytics (
                position_id,
                ticker,
                side,
                entry_stage,
                entry_signal_kind,
                exit_stage,
                exit_signal_kind,
                exit_event,
                exit_reason,
                opened_at,
                closed_at,
                holding_minutes,
                bars_held,
                quantity,
                notional_usd,
                entry_price,
                exit_price,
                realized_pnl_pct,
                realized_pnl_usd,
                mfe_pct,
                mae_pct,
                peak_favorable_price,
                peak_adverse_price,
                intratrade_volatility,
                regime_score_at_entry,
                dom_state_at_entry,
                dom_change_pct_at_entry,
                entry_rank,
                entry_composite_score,
                entry_momentum_z,
                entry_curvature,
                entry_hurst,
                exit_rank,
                exit_composite_score,
                exit_momentum_z,
                exit_curvature,
                exit_hurst,
                take_profit_price_at_exit,
                stop_loss_price_at_exit,
                profit_protection_adjustments,
                minutes_to_first_profit_50bps,
                minutes_to_first_profit_100bps,
                notes,
                post_exit_bars_target,
                post_exit_bars_observed,
                post_exit_completed,
                post_exit_tracking_started_at,
                post_exit_tracking_completed_at,
                post_exit_last_price,
                post_exit_best_price,
                post_exit_worst_price,
                post_exit_favorable_excursion_pct,
                post_exit_adverse_excursion_pct,
                post_exit_volatility
            )
            VALUES (
                :position_id,
                :ticker,
                :side,
                :entry_stage,
                :entry_signal_kind,
                :exit_stage,
                :exit_signal_kind,
                :exit_event,
                :exit_reason,
                :opened_at,
                :closed_at,
                :holding_minutes,
                :bars_held,
                :quantity,
                :notional_usd,
                :entry_price,
                :exit_price,
                :realized_pnl_pct,
                :realized_pnl_usd,
                :mfe_pct,
                :mae_pct,
                :peak_favorable_price,
                :peak_adverse_price,
                :intratrade_volatility,
                :regime_score_at_entry,
                :dom_state_at_entry,
                :dom_change_pct_at_entry,
                :entry_rank,
                :entry_composite_score,
                :entry_momentum_z,
                :entry_curvature,
                :entry_hurst,
                :exit_rank,
                :exit_composite_score,
                :exit_momentum_z,
                :exit_curvature,
                :exit_hurst,
                :take_profit_price_at_exit,
                :stop_loss_price_at_exit,
                :profit_protection_adjustments,
                :minutes_to_first_profit_50bps,
                :minutes_to_first_profit_100bps,
                :notes,
                :post_exit_bars_target,
                :post_exit_bars_observed,
                :post_exit_completed,
                :post_exit_tracking_started_at,
                :post_exit_tracking_completed_at,
                :post_exit_last_price,
                :post_exit_best_price,
                :post_exit_worst_price,
                :post_exit_favorable_excursion_pct,
                :post_exit_adverse_excursion_pct,
                :post_exit_volatility
            )
            """,
            data,
        )
        self._conn.commit()

    async def get_trade_analytics(self, position_id: int) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._get_trade_analytics_sync, position_id)

    def _get_trade_analytics_sync(self, position_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT *
            FROM trade_analytics
            WHERE position_id = ?
            """,
            (position_id,),
        ).fetchone()

    async def list_pending_post_exit_analytics(self) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._list_pending_post_exit_analytics_sync)

    def _list_pending_post_exit_analytics_sync(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            """
            SELECT *
            FROM trade_analytics
            WHERE post_exit_bars_target > 0
              AND post_exit_completed = 0
            ORDER BY closed_at ASC
            """
        ).fetchall()

    async def update_trade_post_exit(
        self,
        *,
        position_id: int,
        post_exit_bars_observed: int,
        post_exit_completed: bool,
        post_exit_tracking_started_at: str | None,
        post_exit_tracking_completed_at: str | None,
        post_exit_last_price: float | None,
        post_exit_best_price: float | None,
        post_exit_worst_price: float | None,
        post_exit_favorable_excursion_pct: float,
        post_exit_adverse_excursion_pct: float,
        post_exit_volatility: float | None,
    ) -> None:
        await asyncio.to_thread(
            self._update_trade_post_exit_sync,
            position_id,
            post_exit_bars_observed,
            post_exit_completed,
            post_exit_tracking_started_at,
            post_exit_tracking_completed_at,
            post_exit_last_price,
            post_exit_best_price,
            post_exit_worst_price,
            post_exit_favorable_excursion_pct,
            post_exit_adverse_excursion_pct,
            post_exit_volatility,
        )

    def _update_trade_post_exit_sync(
        self,
        position_id: int,
        post_exit_bars_observed: int,
        post_exit_completed: bool,
        post_exit_tracking_started_at: str | None,
        post_exit_tracking_completed_at: str | None,
        post_exit_last_price: float | None,
        post_exit_best_price: float | None,
        post_exit_worst_price: float | None,
        post_exit_favorable_excursion_pct: float,
        post_exit_adverse_excursion_pct: float,
        post_exit_volatility: float | None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE trade_analytics
            SET post_exit_bars_observed = ?,
                post_exit_completed = ?,
                post_exit_tracking_started_at = ?,
                post_exit_tracking_completed_at = ?,
                post_exit_last_price = ?,
                post_exit_best_price = ?,
                post_exit_worst_price = ?,
                post_exit_favorable_excursion_pct = ?,
                post_exit_adverse_excursion_pct = ?,
                post_exit_volatility = ?
            WHERE position_id = ?
            """,
            (
                post_exit_bars_observed,
                int(post_exit_completed),
                post_exit_tracking_started_at,
                post_exit_tracking_completed_at,
                post_exit_last_price,
                post_exit_best_price,
                post_exit_worst_price,
                post_exit_favorable_excursion_pct,
                post_exit_adverse_excursion_pct,
                post_exit_volatility,
                position_id,
            ),
        )
        self._conn.commit()

    async def log_portfolio_snapshot(
        self,
        *,
        timestamp: str,
        stage: str,
        event: str,
        wallet_equity_usd: float | None,
        available_balance_usd: float | None,
        open_positions: int,
        gross_notional_usd: float,
        remaining_slots: int | None,
        balance_position_capacity: int | None,
        risk_budget_usd: float | None,
        target_notional_usd: float | None,
        daily_stop_loss_count: int,
        notes: str = "",
    ) -> int:
        return await asyncio.to_thread(
            self._log_portfolio_snapshot_sync,
            timestamp,
            stage,
            event,
            wallet_equity_usd,
            available_balance_usd,
            open_positions,
            gross_notional_usd,
            remaining_slots,
            balance_position_capacity,
            risk_budget_usd,
            target_notional_usd,
            daily_stop_loss_count,
            notes,
        )

    def _log_portfolio_snapshot_sync(
        self,
        timestamp: str,
        stage: str,
        event: str,
        wallet_equity_usd: float | None,
        available_balance_usd: float | None,
        open_positions: int,
        gross_notional_usd: float,
        remaining_slots: int | None,
        balance_position_capacity: int | None,
        risk_budget_usd: float | None,
        target_notional_usd: float | None,
        daily_stop_loss_count: int,
        notes: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO portfolio_snapshots (
                timestamp,
                stage,
                event,
                wallet_equity_usd,
                available_balance_usd,
                open_positions,
                gross_notional_usd,
                remaining_slots,
                balance_position_capacity,
                risk_budget_usd,
                target_notional_usd,
                daily_stop_loss_count,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                stage,
                event,
                wallet_equity_usd,
                available_balance_usd,
                open_positions,
                gross_notional_usd,
                remaining_slots,
                balance_position_capacity,
                risk_budget_usd,
                target_notional_usd,
                daily_stop_loss_count,
                notes,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    async def count_trade_exit_events_for_day(self, exit_event: str, utc_day: str) -> int:
        return await asyncio.to_thread(
            self._count_trade_exit_events_for_day_sync,
            exit_event,
            utc_day,
        )

    def _count_trade_exit_events_for_day_sync(self, exit_event: str, utc_day: str) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*)
            FROM trade_analytics
            WHERE exit_event = ?
              AND substr(closed_at, 1, 10) = ?
            """,
            (exit_event, utc_day),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    async def latest_profitable_trade_close_for_ticker(self, ticker: str) -> str | None:
        row = await self.latest_profitable_trade_state_for_ticker(ticker)
        return str(row["closed_at"]) if row is not None and row["closed_at"] not in (None, "") else None

    async def latest_profitable_trade_state_for_ticker(
        self,
        ticker: str,
    ) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._latest_profitable_trade_state_for_ticker_sync, ticker)

    def _latest_profitable_trade_state_for_ticker_sync(self, ticker: str) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT closed_at, exit_rank, exit_composite_score
            FROM trade_analytics
            WHERE ticker = ?
              AND realized_pnl_usd > 0
            ORDER BY closed_at DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()

    async def count_ticker_losing_trades_for_day(self, ticker: str, utc_day: str) -> int:
        return await asyncio.to_thread(
            self._count_ticker_losing_trades_for_day_sync,
            ticker,
            utc_day,
        )

    def _count_ticker_losing_trades_for_day_sync(self, ticker: str, utc_day: str) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*)
            FROM trade_analytics
            WHERE ticker = ?
              AND realized_pnl_usd < 0
              AND substr(closed_at, 1, 10) = ?
            """,
            (ticker, utc_day),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    async def set_position_pending_exit_reason(
        self,
        position_id: int,
        *,
        pending_exit_reason: str | None,
        updated_at: str,
        notes: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._set_position_pending_exit_reason_sync,
            position_id,
            pending_exit_reason,
            updated_at,
            notes,
        )

    def _set_position_pending_exit_reason_sync(
        self,
        position_id: int,
        pending_exit_reason: str | None,
        updated_at: str,
        notes: str,
    ) -> None:
        self._conn.execute(
            """
            UPDATE positions
            SET pending_exit_reason = ?,
                updated_at = ?,
                notes = CASE
                    WHEN ? = '' THEN notes
                    WHEN notes = '' THEN ?
                    ELSE notes || ' | ' || ?
                END
            WHERE id = ?
            """,
            (
                pending_exit_reason,
                updated_at,
                notes,
                notes,
                notes,
                position_id,
            ),
        )
        self._conn.commit()

    async def summarize_trade_day(self, utc_day: str) -> sqlite3.Row:
        return await asyncio.to_thread(self._summarize_trade_day_sync, utc_day)

    def _summarize_trade_day_sync(self, utc_day: str) -> sqlite3.Row:
        return self._conn.execute(
            """
            SELECT
                COUNT(*) AS trades,
                COALESCE(SUM(CASE WHEN realized_pnl_pct > 0 THEN 1 ELSE 0 END), 0) AS wins,
                COALESCE(SUM(CASE WHEN realized_pnl_pct <= 0 THEN 1 ELSE 0 END), 0) AS losses,
                COALESCE(SUM(CASE WHEN exit_event = 'stop_loss_exit' THEN 1 ELSE 0 END), 0) AS stop_losses,
                COALESCE(SUM(realized_pnl_usd), 0) AS net_pnl_usd,
                AVG(CASE WHEN realized_pnl_pct > 0 THEN realized_pnl_pct END) AS avg_win_pct,
                AVG(CASE WHEN realized_pnl_pct <= 0 THEN realized_pnl_pct END) AS avg_loss_pct
            FROM trade_analytics
            WHERE substr(closed_at, 1, 10) = ?
            """,
            (utc_day,),
        ).fetchone()

    async def log_run_manifest(
        self,
        *,
        run_id: str,
        started_at: str,
        git_commit: str,
        config_fingerprint: str,
        execution_enabled: bool,
        execution_submit_orders: bool,
        demo_mode: bool,
        telegram_enabled: bool,
        operator_pause_new_entries: bool,
        universe_size: int,
        momentum_reference_mode: str,
        cluster_assignment_mode: str,
        notes: str = "",
    ) -> None:
        await asyncio.to_thread(
            self._log_run_manifest_sync,
            run_id,
            started_at,
            git_commit,
            config_fingerprint,
            execution_enabled,
            execution_submit_orders,
            demo_mode,
            telegram_enabled,
            operator_pause_new_entries,
            universe_size,
            momentum_reference_mode,
            cluster_assignment_mode,
            notes,
        )

    def _log_run_manifest_sync(
        self,
        run_id: str,
        started_at: str,
        git_commit: str,
        config_fingerprint: str,
        execution_enabled: bool,
        execution_submit_orders: bool,
        demo_mode: bool,
        telegram_enabled: bool,
        operator_pause_new_entries: bool,
        universe_size: int,
        momentum_reference_mode: str,
        cluster_assignment_mode: str,
        notes: str,
    ) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO run_manifests (
                run_id,
                started_at,
                git_commit,
                config_fingerprint,
                execution_enabled,
                execution_submit_orders,
                demo_mode,
                telegram_enabled,
                operator_pause_new_entries,
                universe_size,
                momentum_reference_mode,
                cluster_assignment_mode,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                started_at,
                git_commit,
                config_fingerprint,
                int(execution_enabled),
                int(execution_submit_orders),
                int(demo_mode),
                int(telegram_enabled),
                int(operator_pause_new_entries),
                universe_size,
                momentum_reference_mode,
                cluster_assignment_mode,
                notes,
            ),
        )
        self._conn.commit()

    async def log_runtime_health_snapshot(self, **snapshot) -> None:
        await asyncio.to_thread(self._log_runtime_health_snapshot_sync, snapshot)

    def _log_runtime_health_snapshot_sync(self, snapshot: dict) -> None:
        self._conn.execute(
            """
            INSERT INTO runtime_health_snapshots (
                run_id,
                recorded_at,
                bootstraps,
                macro_refreshes,
                processed_cycles,
                processed_confirmed_cycles,
                processed_emerging_cycles,
                websocket_sessions,
                websocket_failures,
                queue_drops,
                confirmed_queue_drops,
                emerging_queue_drops,
                confirmed_queue_size,
                emerging_queue_size,
                open_positions,
                daily_stop_loss_count,
                last_bootstrap_at,
                last_macro_refresh_at,
                last_provisional_event_at,
                last_confirmed_event_at,
                last_processed_confirmed_at,
                last_processed_emerging_at,
                last_drift_check_at,
                drift_status,
                operator_pause_new_entries,
                notes
            )
            VALUES (
                :run_id,
                :recorded_at,
                :bootstraps,
                :macro_refreshes,
                :processed_cycles,
                :processed_confirmed_cycles,
                :processed_emerging_cycles,
                :websocket_sessions,
                :websocket_failures,
                :queue_drops,
                :confirmed_queue_drops,
                :emerging_queue_drops,
                :confirmed_queue_size,
                :emerging_queue_size,
                :open_positions,
                :daily_stop_loss_count,
                :last_bootstrap_at,
                :last_macro_refresh_at,
                :last_provisional_event_at,
                :last_confirmed_event_at,
                :last_processed_confirmed_at,
                :last_processed_emerging_at,
                :last_drift_check_at,
                :drift_status,
                :operator_pause_new_entries,
                :notes
            )
            """,
            {
                **snapshot,
                "operator_pause_new_entries": int(bool(snapshot["operator_pause_new_entries"])),
            },
        )
        self._conn.commit()

    async def log_runtime_event(
        self,
        *,
        run_id: str | None,
        created_at: str,
        severity: str,
        component: str,
        event_type: str,
        detail: str,
        stage: str | None = None,
        ticker: str | None = None,
    ) -> int:
        return await asyncio.to_thread(
            self._log_runtime_event_sync,
            run_id,
            created_at,
            severity,
            component,
            event_type,
            detail,
            stage,
            ticker,
        )

    def _log_runtime_event_sync(
        self,
        run_id: str | None,
        created_at: str,
        severity: str,
        component: str,
        event_type: str,
        detail: str,
        stage: str | None,
        ticker: str | None,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO runtime_events (
                run_id,
                created_at,
                severity,
                component,
                event_type,
                stage,
                ticker,
                detail
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, created_at, severity, component, event_type, stage, ticker, detail),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    async def latest_run_manifest(self) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._latest_run_manifest_sync)

    def _latest_run_manifest_sync(self) -> sqlite3.Row | None:
        return self._conn.execute(
            """
            SELECT *
            FROM run_manifests
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()

    async def latest_runtime_health_snapshot(self, run_id: str | None = None) -> sqlite3.Row | None:
        return await asyncio.to_thread(self._latest_runtime_health_snapshot_sync, run_id)

    def _latest_runtime_health_snapshot_sync(self, run_id: str | None) -> sqlite3.Row | None:
        if run_id is None:
            return self._conn.execute(
                """
                SELECT *
                FROM runtime_health_snapshots
                ORDER BY recorded_at DESC
                LIMIT 1
                """
            ).fetchone()
        return self._conn.execute(
            """
            SELECT *
            FROM runtime_health_snapshots
            WHERE run_id = ?
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

    async def list_runtime_events(
        self,
        *,
        run_id: str | None = None,
        limit: int = 20,
    ) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._list_runtime_events_sync, run_id, limit)

    def _list_runtime_events_sync(
        self,
        run_id: str | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        if run_id is None:
            return self._conn.execute(
                """
                SELECT *
                FROM runtime_events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return self._conn.execute(
            """
            SELECT *
            FROM runtime_events
            WHERE run_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()

    async def record_reconciliation_run(
        self,
        *,
        created_at: str,
        window_date: str,
        actual_source: str,
        backtest_source: str,
        actual_entries: int,
        backtest_entries: int,
        matched_entries: int,
        entry_precision: float,
        entry_recall: float,
        actual_exits: int,
        backtest_exits: int,
        matched_exits: int,
        exit_precision: float,
        exit_recall: float,
        matched_exit_reason_count: int,
        mismatched_exit_reason_count: int,
        actual_unique_tickers: int,
        backtest_unique_tickers: int,
        matched_unique_tickers: int,
        ticker_precision: float,
        ticker_recall: float,
        export_dir: str = "",
        notes: str = "",
    ) -> int:
        return await asyncio.to_thread(
            self._record_reconciliation_run_sync,
            created_at,
            window_date,
            actual_source,
            backtest_source,
            actual_entries,
            backtest_entries,
            matched_entries,
            entry_precision,
            entry_recall,
            actual_exits,
            backtest_exits,
            matched_exits,
            exit_precision,
            exit_recall,
            matched_exit_reason_count,
            mismatched_exit_reason_count,
            actual_unique_tickers,
            backtest_unique_tickers,
            matched_unique_tickers,
            ticker_precision,
            ticker_recall,
            export_dir,
            notes,
        )

    def _record_reconciliation_run_sync(
        self,
        created_at: str,
        window_date: str,
        actual_source: str,
        backtest_source: str,
        actual_entries: int,
        backtest_entries: int,
        matched_entries: int,
        entry_precision: float,
        entry_recall: float,
        actual_exits: int,
        backtest_exits: int,
        matched_exits: int,
        exit_precision: float,
        exit_recall: float,
        matched_exit_reason_count: int,
        mismatched_exit_reason_count: int,
        actual_unique_tickers: int,
        backtest_unique_tickers: int,
        matched_unique_tickers: int,
        ticker_precision: float,
        ticker_recall: float,
        export_dir: str,
        notes: str,
    ) -> int:
        cursor = self._conn.execute(
            """
            INSERT INTO reconciliation_runs (
                created_at,
                window_date,
                actual_source,
                backtest_source,
                actual_entries,
                backtest_entries,
                matched_entries,
                entry_precision,
                entry_recall,
                actual_exits,
                backtest_exits,
                matched_exits,
                exit_precision,
                exit_recall,
                matched_exit_reason_count,
                mismatched_exit_reason_count,
                actual_unique_tickers,
                backtest_unique_tickers,
                matched_unique_tickers,
                ticker_precision,
                ticker_recall,
                export_dir,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                window_date,
                actual_source,
                backtest_source,
                actual_entries,
                backtest_entries,
                matched_entries,
                entry_precision,
                entry_recall,
                actual_exits,
                backtest_exits,
                matched_exits,
                exit_precision,
                exit_recall,
                matched_exit_reason_count,
                mismatched_exit_reason_count,
                actual_unique_tickers,
                backtest_unique_tickers,
                matched_unique_tickers,
                ticker_precision,
                ticker_recall,
                export_dir,
                notes,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def close(self) -> None:
        self._conn.close()
