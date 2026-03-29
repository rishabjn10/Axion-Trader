"""
SQLite3 persistence layer for axion-trader.

Provides a complete data access layer using Python's built-in sqlite3 module.
No external ORM is used — raw SQL with parameterised queries ensures maximum
control over performance and schema.

Schema overview:
  decisions   — One record per trading cycle: all indicators, AI decisions, risk outcomes
  trades      — One record per executed order with PnL tracking
  portfolio_snapshots — Point-in-time portfolio value snapshots
  agent_state — Key-value store for runtime state (circuit breaker, regime, etc.)

Thread safety: All functions acquire a connection per call rather than sharing
a global connection, ensuring thread-safety when the FastAPI server and agent
loop run concurrently. SQLite WAL mode is enabled for better concurrent reads.

Role in system: The single source of truth for all historical data. The API
routes query this module for dashboard data; the agent loop writes here after
every decision and trade; risk guard reads here for circuit breaker state.

Dependencies: sqlite3 (stdlib), json (stdlib), math (stdlib), loguru
"""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from backend.config.settings import settings

# Database file path — defined in settings, stored at backend/data/trading.db
DB_PATH: Path = settings.db_path


def _get_connection() -> sqlite3.Connection:
    """
    Open and configure a SQLite3 connection.

    Enables WAL journaling mode for concurrent read/write access from the
    API server and agent loop. Row factory set to sqlite3.Row for dict-like
    access to query results.

    Returns:
        Configured sqlite3.Connection object. Caller is responsible for closing.

    Example:
        >>> conn = _get_connection()
        >>> try:
        ...     cursor = conn.execute("SELECT * FROM trades")
        ... finally:
        ...     conn.close()
    """
    # Ensure the data directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row

    # WAL mode: readers don't block writers, writers don't block readers
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")  # Balance durability and speed

    return conn


def init_db() -> None:
    """
    Create all database tables if they don't already exist.

    Idempotent: safe to call multiple times. Uses CREATE TABLE IF NOT EXISTS.
    Should be called once at agent startup before any other store operations.

    Raises:
        sqlite3.Error: If the database file cannot be created or schema fails.

    Example:
        >>> init_db()
        >>> logger.info("Database ready")
    """
    conn = _get_connection()
    try:
        cursor = conn.cursor()

        # ── decisions table ────────────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp             TEXT NOT NULL,
                pair                  TEXT NOT NULL,
                timeframe             INTEGER NOT NULL DEFAULT 60,
                rsi                   REAL,
                macd_cross            TEXT,
                bb_position           TEXT,
                confluence_score      INTEGER,
                llm_action            TEXT,
                llm_confidence        REAL,
                llm_reasoning         TEXT,
                rule_action           TEXT,
                rule_confidence       REAL,
                rule_triggered        TEXT,
                final_action          TEXT,
                final_confidence      REAL,
                consensus_reached     INTEGER,
                approved_by_risk      INTEGER,
                risk_rejection_reason TEXT,
                mode                  TEXT DEFAULT 'paper'
            )
        """)

        # ── trades table ───────────────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id          TEXT UNIQUE NOT NULL,
                timestamp         TEXT NOT NULL,
                pair              TEXT NOT NULL,
                action            TEXT NOT NULL,
                volume            REAL NOT NULL DEFAULT 0.0,
                entry_price       REAL NOT NULL DEFAULT 0.0,
                exit_price        REAL,
                pnl_usd           REAL,
                pnl_pct           REAL,
                status            TEXT NOT NULL DEFAULT 'open',
                stop_price        REAL DEFAULT 0.0,
                take_profit_price REAL DEFAULT 0.0,
                mode              TEXT DEFAULT 'paper',
                closed_at         TEXT,
                llm_reasoning     TEXT
            )
        """)

        # ── portfolio_snapshots table ──────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp            TEXT NOT NULL,
                total_value_usd      REAL NOT NULL DEFAULT 0.0,
                btc_balance          REAL DEFAULT 0.0,
                usd_balance          REAL DEFAULT 0.0,
                open_positions_count INTEGER DEFAULT 0,
                daily_pnl_usd        REAL DEFAULT 0.0,
                daily_pnl_pct        REAL DEFAULT 0.0
            )
        """)

        # ── agent_state table ──────────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_state (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        # ── execution_quality table ────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS execution_quality (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id     TEXT UNIQUE NOT NULL,
                timestamp    TEXT NOT NULL,
                pair         TEXT NOT NULL,
                action       TEXT NOT NULL,
                signal_price REAL NOT NULL DEFAULT 0.0,
                entry_price  REAL NOT NULL DEFAULT 0.0,
                slippage_pct REAL NOT NULL DEFAULT 0.0,
                order_type   TEXT NOT NULL DEFAULT 'market'
            )
        """)

        # ── Indexes for query performance ──────────────────────────────────────
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_decisions_timestamp ON decisions(timestamp DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp DESC)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON portfolio_snapshots(timestamp DESC)"
        )

        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_exec_quality_timestamp ON execution_quality(timestamp DESC)"
        )

        conn.commit()

        # ── Migrations: add columns that may not exist in older DBs ───────────
        for migration in [
            "ALTER TABLE decisions ADD COLUMN confluence_breakdown TEXT",
        ]:
            try:
                cursor.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

        logger.info(f"Database initialised at {DB_PATH}")

    except sqlite3.Error as exc:
        logger.error(f"Database initialisation failed: {exc}")
        raise
    finally:
        conn.close()


def save_decision(d: dict[str, Any]) -> None:
    """
    Save a complete trading decision record to the decisions table.

    Args:
        d: Dictionary with decision fields matching the decisions table schema.
           Required keys: timestamp, pair, final_action, final_confidence.
           Optional keys default to None.

    Example:
        >>> save_decision({'timestamp': '2024-01-01T00:00:00', 'pair': 'BTCUSD',
        ...                'final_action': 'buy', 'final_confidence': 0.82, ...})
    """
    conn = _get_connection()
    try:
        conn.execute("""
            INSERT INTO decisions (
                timestamp, pair, timeframe, rsi, macd_cross, bb_position,
                confluence_score, llm_action, llm_confidence, llm_reasoning,
                rule_action, rule_confidence, rule_triggered,
                final_action, final_confidence, consensus_reached,
                approved_by_risk, risk_rejection_reason, mode,
                confluence_breakdown
            ) VALUES (
                :timestamp, :pair, :timeframe, :rsi, :macd_cross, :bb_position,
                :confluence_score, :llm_action, :llm_confidence, :llm_reasoning,
                :rule_action, :rule_confidence, :rule_triggered,
                :final_action, :final_confidence, :consensus_reached,
                :approved_by_risk, :risk_rejection_reason, :mode,
                :confluence_breakdown
            )
        """, {
            "timestamp": d.get("timestamp", datetime.now(UTC).isoformat()),
            "pair": d.get("pair", settings.trading_pair),
            "timeframe": d.get("timeframe", 60),
            "rsi": d.get("rsi"),
            "macd_cross": d.get("macd_cross"),
            "bb_position": d.get("bb_position"),
            "confluence_score": d.get("confluence_score"),
            "llm_action": d.get("llm_action"),
            "llm_confidence": d.get("llm_confidence"),
            "llm_reasoning": d.get("llm_reasoning"),
            "rule_action": d.get("rule_action"),
            "rule_confidence": d.get("rule_confidence"),
            "rule_triggered": d.get("rule_triggered"),
            "final_action": d.get("final_action"),
            "final_confidence": d.get("final_confidence"),
            "consensus_reached": 1 if d.get("consensus_reached") else 0,
            "approved_by_risk": 1 if d.get("approved_by_risk") else 0,
            "risk_rejection_reason": d.get("risk_rejection_reason"),
            "mode": d.get("mode", settings.trading_mode),
            "confluence_breakdown": d.get("confluence_breakdown"),
        })
        conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"Failed to save decision: {exc}")
    finally:
        conn.close()


def save_trade(t: dict[str, Any]) -> None:
    """
    Insert a new trade record into the trades table.

    Uses INSERT OR REPLACE to handle the case where the same order_id is
    submitted twice (e.g. retry after timeout).

    Args:
        t: Dictionary with trade fields. Required: order_id, timestamp, pair,
           action, volume, entry_price. Optional fields default to None/0.

    Example:
        >>> save_trade({'order_id': 'XYZ123', 'action': 'buy', 'pair': 'BTCUSD',
        ...             'volume': 0.001, 'entry_price': 67000.0, ...})
    """
    conn = _get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO trades (
                order_id, timestamp, pair, action, volume, entry_price,
                exit_price, pnl_usd, pnl_pct, status, stop_price,
                take_profit_price, mode, closed_at, llm_reasoning
            ) VALUES (
                :order_id, :timestamp, :pair, :action, :volume, :entry_price,
                :exit_price, :pnl_usd, :pnl_pct, :status, :stop_price,
                :take_profit_price, :mode, :closed_at, :llm_reasoning
            )
        """, {
            "order_id": t["order_id"],
            "timestamp": t.get("timestamp", datetime.now(UTC).isoformat()),
            "pair": t.get("pair", settings.trading_pair),
            "action": t["action"],
            "volume": t.get("volume", 0.0),
            "entry_price": t.get("entry_price", 0.0),
            "exit_price": t.get("exit_price"),
            "pnl_usd": t.get("pnl_usd"),
            "pnl_pct": t.get("pnl_pct"),
            "status": t.get("status", "open"),
            "stop_price": t.get("stop_price", 0.0),
            "take_profit_price": t.get("take_profit_price", 0.0),
            "mode": t.get("mode", settings.trading_mode),
            "closed_at": t.get("closed_at"),
            "llm_reasoning": t.get("llm_reasoning"),
        })
        conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"Failed to save trade: {exc}")
    finally:
        conn.close()


def update_trade_exit(
    order_id: str,
    exit_price: float,
    pnl_usd: float,
    pnl_pct: float,
    closed_at: str,
) -> None:
    """
    Update an existing trade record with exit price and PnL data.

    Called when a position is closed to record the outcome. Sets status
    to 'closed' and records the exit timestamp.

    Args:
        order_id: Kraken order ID of the trade to update.
        exit_price: Price at which the position was closed.
        pnl_usd: Profit/loss in USD (positive = profit).
        pnl_pct: Profit/loss as a percentage of entry price.
        closed_at: ISO 8601 UTC timestamp when position was closed.

    Example:
        >>> update_trade_exit('XYZ123', 68000.0, 100.0, 1.49, '2024-01-01T12:00:00Z')
    """
    conn = _get_connection()
    try:
        conn.execute("""
            UPDATE trades
            SET exit_price = ?, pnl_usd = ?, pnl_pct = ?, status = 'closed', closed_at = ?
            WHERE order_id = ?
        """, (exit_price, pnl_usd, pnl_pct, closed_at, order_id))
        conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"Failed to update trade exit for {order_id}: {exc}")
    finally:
        conn.close()


def save_portfolio_snapshot(p: dict[str, Any]) -> None:
    """
    Save a portfolio value snapshot to the portfolio_snapshots table.

    Called at the end of each standard cycle to track portfolio evolution
    over time. Used for PnL charting and Sharpe ratio calculation.

    Args:
        p: Dictionary with portfolio snapshot fields:
           total_value_usd, btc_balance, usd_balance,
           open_positions_count, daily_pnl_usd, daily_pnl_pct.

    Example:
        >>> save_portfolio_snapshot({'total_value_usd': 100000.0, 'btc_balance': 0.5, ...})
    """
    conn = _get_connection()
    try:
        conn.execute("""
            INSERT INTO portfolio_snapshots (
                timestamp, total_value_usd, btc_balance, usd_balance,
                open_positions_count, daily_pnl_usd, daily_pnl_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            p.get("timestamp", datetime.now(UTC).isoformat()),
            p.get("total_value_usd", 0.0),
            p.get("btc_balance", 0.0),
            p.get("usd_balance", 0.0),
            p.get("open_positions_count", 0),
            p.get("daily_pnl_usd", 0.0),
            p.get("daily_pnl_pct", 0.0),
        ))
        conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"Failed to save portfolio snapshot: {exc}")
    finally:
        conn.close()


def get_recent_trades(limit: int = 50) -> list[dict[str, Any]]:
    """
    Fetch the most recent trades ordered newest-first.

    Args:
        limit: Maximum number of trades to return. Defaults to 50.

    Returns:
        List of trade dictionaries. Empty list if no trades exist.

    Example:
        >>> trades = get_recent_trades(limit=10)
        >>> for t in trades:
        ...     print(t['order_id'], t['pnl_pct'])
    """
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        logger.error(f"Failed to fetch recent trades: {exc}")
        return []
    finally:
        conn.close()


def get_recent_decisions(limit: int = 100) -> list[dict[str, Any]]:
    """
    Fetch the most recent decision records ordered newest-first.

    Args:
        limit: Maximum number of decisions to return. Defaults to 100.

    Returns:
        List of decision dictionaries.

    Example:
        >>> decisions = get_recent_decisions(50)
        >>> print(decisions[0]['final_action'])
    """
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT * FROM decisions ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )
        return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as exc:
        logger.error(f"Failed to fetch recent decisions: {exc}")
        return []
    finally:
        conn.close()


def get_state(key: str) -> str | None:
    """
    Read a value from the key-value agent_state table.

    Args:
        key: State key to look up.

    Returns:
        Value string if key exists, None otherwise.

    Example:
        >>> active = get_state('circuit_breaker_active')
        >>> print(active)  # 'true' or 'false' or None
    """
    conn = _get_connection()
    try:
        cursor = conn.execute(
            "SELECT value FROM agent_state WHERE key = ?",
            (key,)
        )
        row = cursor.fetchone()
        return row["value"] if row else None
    except sqlite3.Error as exc:
        logger.error(f"Failed to get state '{key}': {exc}")
        return None
    finally:
        conn.close()


def set_state(key: str, value: str) -> None:
    """
    Write a value to the key-value agent_state table (upsert).

    Args:
        key: State key to set.
        value: String value to store.

    Example:
        >>> set_state('circuit_breaker_active', 'true')
        >>> set_state('last_regime', 'TRENDING_UP')
    """
    conn = _get_connection()
    try:
        conn.execute("""
            INSERT INTO agent_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (key, value, datetime.now(UTC).isoformat()))
        conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"Failed to set state '{key}': {exc}")
    finally:
        conn.close()


def save_execution_quality(q: dict[str, Any]) -> None:
    """
    Save an execution quality record to the execution_quality table.

    Called after every successful order to track signal price vs actual
    entry price for slippage analysis and execution quality reporting.

    Args:
        q: Dictionary with keys: order_id, timestamp, pair, action,
           signal_price, entry_price, slippage_pct, order_type.

    Example:
        >>> save_execution_quality({
        ...     'order_id': 'XYZ123', 'pair': 'BTCUSD', 'action': 'buy',
        ...     'signal_price': 67000.0, 'entry_price': 66800.0,
        ...     'slippage_pct': -0.299, 'order_type': 'limit'
        ... })
    """
    conn = _get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO execution_quality (
                order_id, timestamp, pair, action,
                signal_price, entry_price, slippage_pct, order_type
            ) VALUES (
                :order_id, :timestamp, :pair, :action,
                :signal_price, :entry_price, :slippage_pct, :order_type
            )
        """, {
            "order_id":     q["order_id"],
            "timestamp":    q.get("timestamp", datetime.now(UTC).isoformat()),
            "pair":         q.get("pair", settings.trading_pair),
            "action":       q["action"],
            "signal_price": q.get("signal_price", 0.0),
            "entry_price":  q.get("entry_price", 0.0),
            "slippage_pct": q.get("slippage_pct", 0.0),
            "order_type":   q.get("order_type", "market"),
        })
        conn.commit()
    except sqlite3.Error as exc:
        logger.error(f"Failed to save execution quality for {q.get('order_id')}: {exc}")
    finally:
        conn.close()


def compute_metrics() -> dict[str, Any]:
    """
    Compute portfolio performance metrics from historical trade data.

    Calculates:
    - Total PnL (USD and percentage)
    - Sharpe ratio (annualised using daily returns)
    - Maximum drawdown (as percentage of portfolio peak)
    - Win rate and trade counts

    Sharpe ratio formula:
        annualised_sharpe = (mean_daily_return / std_daily_return) * sqrt(365)
        Uses daily PnL snapshots rather than individual trade PnLs.

    Max drawdown formula:
        For each snapshot: drawdown = (peak_value - current_value) / peak_value
        Max drawdown = maximum of all drawdown values

    Returns:
        Dictionary with metrics keys:
          total_pnl_usd, total_pnl_pct, sharpe_ratio, max_drawdown_pct,
          win_rate_pct, total_trades, winning_trades, losing_trades,
          open_positions, portfolio_value_usd, daily_pnl_usd, daily_pnl_pct

    Example:
        >>> metrics = compute_metrics()
        >>> print(f"Sharpe: {metrics['sharpe_ratio']:.2f}")
    """
    conn = _get_connection()
    try:
        # ── Trade statistics ───────────────────────────────────────────────────
        trades_cursor = conn.execute(
            "SELECT pnl_usd, pnl_pct, status, entry_price FROM trades"
        )
        all_trades = trades_cursor.fetchall()

        total_trades = len(all_trades)
        closed_trades = [t for t in all_trades if t["status"] == "closed"]
        open_trades = [t for t in all_trades if t["status"] == "open"]

        winning_trades = sum(1 for t in closed_trades if (t["pnl_usd"] or 0) > 0)
        losing_trades = sum(1 for t in closed_trades if (t["pnl_usd"] or 0) < 0)
        total_pnl_usd = sum((t["pnl_usd"] or 0) for t in closed_trades)

        win_rate_pct = (
            (winning_trades / len(closed_trades) * 100) if closed_trades else 0.0
        )

        # ── Latest portfolio snapshot ─────────────────────────────────────────
        snap_cursor = conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT 1"
        )
        latest_snap = snap_cursor.fetchone()
        portfolio_value_usd = float(latest_snap["total_value_usd"]) if latest_snap else 0.0
        daily_pnl_usd = float(latest_snap["daily_pnl_usd"]) if latest_snap else 0.0
        daily_pnl_pct = float(latest_snap["daily_pnl_pct"]) if latest_snap else 0.0

        # Calculate total_pnl_pct from initial portfolio value
        # First snapshot is the baseline
        first_snap_cursor = conn.execute(
            "SELECT total_value_usd FROM portfolio_snapshots ORDER BY timestamp ASC LIMIT 1"
        )
        first_snap = first_snap_cursor.fetchone()
        initial_value = float(first_snap["total_value_usd"]) if first_snap else portfolio_value_usd

        total_pnl_pct = (
            ((portfolio_value_usd - initial_value) / initial_value * 100)
            if initial_value > 0
            else 0.0
        )

        # ── Sharpe ratio from daily returns ───────────────────────────────────
        # Fetch all portfolio snapshots to compute daily returns
        daily_cursor = conn.execute("""
            SELECT DATE(timestamp) as day, AVG(daily_pnl_pct) as avg_daily_pnl
            FROM portfolio_snapshots
            GROUP BY DATE(timestamp)
            ORDER BY day ASC
        """)
        daily_rows = daily_cursor.fetchall()
        daily_returns = [float(r["avg_daily_pnl"]) for r in daily_rows if r["avg_daily_pnl"] is not None]

        sharpe_ratio = 0.0
        if len(daily_returns) >= 5:  # Need at least 5 data points for meaningful Sharpe
            mean_return = sum(daily_returns) / len(daily_returns)
            if len(daily_returns) > 1:
                variance = sum((r - mean_return) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
                std_return = math.sqrt(variance)
                if std_return > 0:
                    # Annualised Sharpe: multiply by sqrt(365) for daily data
                    sharpe_ratio = round((mean_return / std_return) * math.sqrt(365), 4)

        # ── Maximum drawdown ──────────────────────────────────────────────────
        snap_all_cursor = conn.execute(
            "SELECT total_value_usd FROM portfolio_snapshots ORDER BY timestamp ASC"
        )
        all_values = [float(r["total_value_usd"]) for r in snap_all_cursor.fetchall()]

        max_drawdown_pct = 0.0
        if all_values:
            peak = all_values[0]
            for val in all_values:
                if val > peak:
                    peak = val
                if peak > 0:
                    drawdown = (peak - val) / peak * 100
                    max_drawdown_pct = max(max_drawdown_pct, drawdown)

        return {
            "total_pnl_usd": round(total_pnl_usd, 2),
            "total_pnl_pct": round(total_pnl_pct, 4),
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "win_rate_pct": round(win_rate_pct, 2),
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "open_positions": len(open_trades),
            "portfolio_value_usd": round(portfolio_value_usd, 2),
            "daily_pnl_usd": round(daily_pnl_usd, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 4),
        }

    except sqlite3.Error as exc:
        logger.error(f"Failed to compute metrics: {exc}")
        return {
            "total_pnl_usd": 0.0,
            "total_pnl_pct": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate_pct": 0.0,
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "open_positions": 0,
            "portfolio_value_usd": 0.0,
            "daily_pnl_usd": 0.0,
            "daily_pnl_pct": 0.0,
        }
    finally:
        conn.close()
