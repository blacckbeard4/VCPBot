"""VCPBot SQLite database — schema, connection management, CRUD helpers.

Uses WAL mode for concurrent reads. All timestamps in ISO format (US/Eastern).
"""

import csv
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from config import DB_PATH, TRADE_LOG_CSV, TIMEZONE, ACCOUNT_VALUE

logger = logging.getLogger(__name__)

ET = ZoneInfo(TIMEZONE)


# ─── Connection ─────────────────────────────────────────────


def get_conn() -> sqlite3.Connection:
    """Return a new SQLite connection with WAL mode and Row factory."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ─── Schema ─────────────────────────────────────────────────

_SCHEMA_SQL = """
-- trades: central trade lifecycle table
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL,
    direction           TEXT NOT NULL DEFAULT 'LONG',
    entry_price         REAL,
    stop_price          REAL NOT NULL,
    target_1_price      REAL NOT NULL,
    shares              REAL NOT NULL,
    entry_date          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'PENDING',
    exit_price          REAL,
    exit_date           TEXT,
    exit_reason         TEXT,
    pnl                 REAL,
    r_multiple          REAL,
    -- VCP-specific fields
    pivot_price         REAL,
    rs_rank             REAL,
    base_duration_weeks INTEGER,
    contraction_depth_pct REAL,
    alpaca_order_id     TEXT,
    regime_at_entry     TEXT,
    analyst_rationale   TEXT,
    created_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_entry_date ON trades(entry_date);

-- scan_log: one row per pipeline run
CREATE TABLE IF NOT EXISTS scan_log (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date                TEXT NOT NULL,
    regime                  TEXT NOT NULL,
    tickers_scanned         INTEGER,
    tickers_phase2          INTEGER,
    tickers_phase3          INTEGER,
    vcp_setups              INTEGER,
    orders_queued           INTEGER,
    timestamp               TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scan_log_run_date ON scan_log(run_date);

-- portfolio_state: daily portfolio snapshots
CREATE TABLE IF NOT EXISTS portfolio_state (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    date                    TEXT NOT NULL,
    account_value           REAL,
    cash_available          REAL,
    open_positions          INTEGER,
    total_unrealized_pnl    REAL,
    peak_account_value      REAL,
    current_drawdown_pct    REAL,
    regime                  TEXT,
    timestamp               TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_portfolio_state_date ON portfolio_state(date);

-- regime_state: persists Cash Mode / FTD Mode across restarts
CREATE TABLE IF NOT EXISTS regime_state (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    cash_mode           INTEGER NOT NULL DEFAULT 0,
    ftd_mode            INTEGER NOT NULL DEFAULT 0,
    rally_day1_low      REAL,
    rally_day1_date     TEXT,
    rally_day_count     INTEGER DEFAULT 0,
    spy_close           REAL,
    spy_sma200          REAL,
    ftd_date            TEXT,
    updated_at          TEXT DEFAULT (datetime('now'))
);

-- errors: error logging for all pipeline steps
CREATE TABLE IF NOT EXISTS errors (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    step                TEXT NOT NULL,
    ticker              TEXT,
    error_type          TEXT NOT NULL,
    error_message       TEXT NOT NULL,
    traceback_str       TEXT,
    timestamp           TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_errors_step ON errors(step);

-- ticker_rejections: per-ticker rejection traces for Phase 3, VCP, and Risk phases
CREATE TABLE IF NOT EXISTS ticker_rejections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date    TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    phase       TEXT NOT NULL,   -- 'PHASE3', 'VCP', 'RISK'
    reason      TEXT NOT NULL,
    timestamp   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rejections_run_date ON ticker_rejections(run_date);
CREATE INDEX IF NOT EXISTS idx_rejections_ticker ON ticker_rejections(ticker);
"""


def init_db() -> None:
    """Create all tables and indexes if they don't exist."""
    conn = get_conn()
    with conn:
        conn.executescript(_SCHEMA_SQL)
        # Idempotent migration: add ftd_date if not present (existing DBs)
        try:
            conn.execute("ALTER TABLE regime_state ADD COLUMN ftd_date TEXT")
            logger.info("Migrated regime_state: added ftd_date column")
        except Exception:
            pass  # column already exists
        # Migration: create ticker_rejections if it doesn't exist (existing DBs)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ticker_rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL, ticker TEXT NOT NULL,
                    phase TEXT NOT NULL, reason TEXT NOT NULL,
                    timestamp TEXT DEFAULT (datetime('now'))
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rejections_run_date ON ticker_rejections(run_date)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_rejections_ticker ON ticker_rejections(ticker)")
        except Exception:
            pass
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


# ─── trades CRUD ────────────────────────────────────────────


def insert_trade(
    ticker: str,
    stop_price: float,
    target_1_price: float,
    shares: float,
    entry_date: str,
    pivot_price: float,
    rs_rank: float,
    base_duration_weeks: int,
    contraction_depth_pct: float,
    regime_at_entry: str,
    analyst_rationale: str = "",
    status: str = "PENDING",
    entry_price: Optional[float] = None,
    direction: str = "LONG",
) -> int:
    """Insert a trade row. Returns the trade id."""
    conn = get_conn()
    with conn:
        cursor = conn.execute(
            """INSERT INTO trades
               (ticker, direction, entry_price, stop_price, target_1_price,
                shares, entry_date, status, pivot_price, rs_rank,
                base_duration_weeks, contraction_depth_pct, regime_at_entry, analyst_rationale)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, direction, entry_price, stop_price, target_1_price,
             shares, entry_date, status, pivot_price, rs_rank,
             base_duration_weeks, contraction_depth_pct, regime_at_entry, analyst_rationale),
        )
        trade_id = cursor.lastrowid
    conn.close()
    logger.info("Inserted trade %d: %s %d shares, pivot=%.2f", trade_id, ticker, shares, pivot_price)
    return trade_id


def update_trade_status(trade_id: int, status: str, **kwargs) -> None:
    """Update trade status and any additional fields.

    kwargs can include: exit_price, exit_date, exit_reason, pnl, r_multiple,
    entry_price, alpaca_order_id, stop_price.
    """
    set_clauses = ["status = ?"]
    values: list = [status]
    for col, val in kwargs.items():
        set_clauses.append(f"{col} = ?")
        values.append(val)
    values.append(trade_id)
    sql = f"UPDATE trades SET {', '.join(set_clauses)} WHERE id = ?"
    conn = get_conn()
    with conn:
        conn.execute(sql, values)
    conn.close()
    logger.info("Trade %d → %s %s", trade_id, status,
                dict(kwargs) if kwargs else "")


def get_open_trades() -> list[sqlite3.Row]:
    """Return trades with status OPEN."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY entry_date"
    ).fetchall()
    conn.close()
    return rows


def get_pending_trades() -> list[sqlite3.Row]:
    """Return trades with status PENDING (setups queued for next morning)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'PENDING' ORDER BY rs_rank DESC"
    ).fetchall()
    conn.close()
    return rows


def get_placed_trades() -> list[sqlite3.Row]:
    """Return trades with status PLACED (buy stop orders sitting on Alpaca)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'PLACED' ORDER BY entry_date"
    ).fetchall()
    conn.close()
    return rows


def get_trades_by_date_range(start: str, end: str) -> list[sqlite3.Row]:
    """Return trades with entry_date between start and end (inclusive)."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trades WHERE entry_date BETWEEN ? AND ? ORDER BY entry_date",
        (start, end),
    ).fetchall()
    conn.close()
    return rows


def get_all_closed_trades() -> list[sqlite3.Row]:
    """Return all closed trades (STOPPED, TARGET_HIT, CANCELLED, EXPIRED)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM trades
           WHERE status IN ('STOPPED', 'TARGET_HIT', 'CANCELLED', 'EXPIRED', 'GAP_CANCELLED', 'RVOL_CANCELLED')
           ORDER BY exit_date DESC"""
    ).fetchall()
    conn.close()
    return rows


# ─── scan_log CRUD ──────────────────────────────────────────


def insert_scan_log(
    run_date: str,
    regime: str,
    tickers_scanned: int,
    tickers_phase2: int,
    tickers_phase3: int,
    vcp_setups: int,
    orders_queued: int,
) -> int:
    """Log a pipeline run summary. Returns scan_log id."""
    conn = get_conn()
    with conn:
        cursor = conn.execute(
            """INSERT INTO scan_log
               (run_date, regime, tickers_scanned, tickers_phase2,
                tickers_phase3, vcp_setups, orders_queued)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_date, regime, tickers_scanned, tickers_phase2,
             tickers_phase3, vcp_setups, orders_queued),
        )
        scan_id = cursor.lastrowid
    conn.close()
    return scan_id


# ─── portfolio_state CRUD ───────────────────────────────────


def insert_portfolio_state(
    date: str,
    account_value: float,
    cash_available: float,
    open_positions: int,
    total_unrealized_pnl: float,
    peak_account_value: float,
    current_drawdown_pct: float,
    regime: str = "",
) -> None:
    """Snapshot current portfolio state."""
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO portfolio_state
               (date, account_value, cash_available, open_positions,
                total_unrealized_pnl, peak_account_value, current_drawdown_pct, regime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (date, account_value, cash_available, open_positions,
             total_unrealized_pnl, peak_account_value, current_drawdown_pct, regime),
        )
    conn.close()


def get_latest_portfolio_state() -> Optional[sqlite3.Row]:
    """Return the most recent portfolio snapshot, or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM portfolio_state ORDER BY date DESC, id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row


def get_rolling_peak_account_value(days: int = 60) -> float:
    """Return peak account value over the last N days."""
    conn = get_conn()
    row = conn.execute(
        """SELECT MAX(account_value) AS peak FROM portfolio_state
           WHERE date >= date('now', ?)""",
        (f"-{days} days",),
    ).fetchone()
    conn.close()
    if row and row["peak"] is not None:
        return row["peak"]
    return ACCOUNT_VALUE


# ─── regime_state CRUD ──────────────────────────────────────


def get_regime_state() -> Optional[sqlite3.Row]:
    """Return the most recent regime state row."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM regime_state ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row


def upsert_regime_state(
    cash_mode: bool,
    ftd_mode: bool,
    spy_close: float,
    spy_sma200: float,
    rally_day1_low: Optional[float] = None,
    rally_day1_date: Optional[str] = None,
    rally_day_count: int = 0,
    ftd_date: Optional[str] = None,
) -> None:
    """Insert a new regime_state row (history is kept for audit)."""
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO regime_state
               (cash_mode, ftd_mode, spy_close, spy_sma200,
                rally_day1_low, rally_day1_date, rally_day_count, ftd_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (int(cash_mode), int(ftd_mode), spy_close, spy_sma200,
             rally_day1_low, rally_day1_date, rally_day_count, ftd_date),
        )
    conn.close()


# ─── ticker_rejections CRUD ─────────────────────────────────


def bulk_insert_rejections(run_date: str, rejections: list[dict]) -> None:
    """Insert multiple rejection trace rows in one transaction.

    Each dict must have: ticker (str), phase (str), reason (str).
    """
    if not rejections:
        return
    conn = get_conn()
    with conn:
        conn.executemany(
            "INSERT INTO ticker_rejections (run_date, ticker, phase, reason) VALUES (?, ?, ?, ?)",
            [(run_date, r["ticker"], r["phase"], r["reason"]) for r in rejections],
        )
    conn.close()
    logger.debug("Logged %d rejection traces for %s", len(rejections), run_date)


def get_rejections_for_date(run_date: str) -> list[sqlite3.Row]:
    """Return all rejection traces for a given run date."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM ticker_rejections WHERE run_date = ? ORDER BY phase, ticker",
        (run_date,),
    ).fetchall()
    conn.close()
    return rows


# ─── error logging ──────────────────────────────────────────


def log_error(
    step: str,
    error_type: str,
    error_message: str,
    ticker: Optional[str] = None,
    traceback_str: Optional[str] = None,
) -> None:
    """Log an error to the errors table."""
    conn = get_conn()
    with conn:
        conn.execute(
            """INSERT INTO errors (step, ticker, error_type, error_message, traceback_str)
               VALUES (?, ?, ?, ?, ?)""",
            (step, ticker, error_type, error_message, traceback_str),
        )
    conn.close()
    logger.error("[%s] %s: %s (ticker=%s)", step, error_type, error_message, ticker)


# ─── CSV trade log ───────────────────────────────────────────

_CSV_HEADERS = [
    "date", "ticker", "entry_price", "stop_price", "target_price",
    "shares", "account_equity_at_entry", "exit_date", "exit_price",
    "exit_reason", "pnl_dollars", "pnl_pct", "r_multiple",
    "rs_rank", "base_weeks", "contraction_depth_pct",
]


def _ensure_csv_header() -> None:
    """Write header row if trade_log.csv doesn't exist."""
    if not TRADE_LOG_CSV.exists():
        with open(TRADE_LOG_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS)
            writer.writeheader()


def log_trade_to_csv(
    ticker: str,
    entry_price: float,
    stop_price: float,
    target_price: float,
    shares: float,
    account_equity: float,
    rs_rank: float,
    base_weeks: int,
    contraction_depth_pct: float,
    exit_date: str = "",
    exit_price: float = 0.0,
    exit_reason: str = "",
    pnl_dollars: float = 0.0,
    pnl_pct: float = 0.0,
    r_multiple: float = 0.0,
) -> None:
    """Append a trade entry/exit row to the CSV trade log."""
    try:
        _ensure_csv_header()
        with open(TRADE_LOG_CSV, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_HEADERS)
            writer.writerow({
                "date": datetime.now(ET).strftime("%Y-%m-%d"),
                "ticker": ticker,
                "entry_price": round(entry_price, 2),
                "stop_price": round(stop_price, 2),
                "target_price": round(target_price, 2),
                "shares": shares,
                "account_equity_at_entry": round(account_equity, 2),
                "exit_date": exit_date,
                "exit_price": round(exit_price, 2) if exit_price else "",
                "exit_reason": exit_reason,
                "pnl_dollars": round(pnl_dollars, 2) if pnl_dollars else "",
                "pnl_pct": round(pnl_pct * 100, 2) if pnl_pct else "",
                "r_multiple": round(r_multiple, 2) if r_multiple else "",
                "rs_rank": round(rs_rank, 1),
                "base_weeks": base_weeks,
                "contraction_depth_pct": round(contraction_depth_pct * 100, 1),
            })
    except Exception as e:
        logger.warning("Failed to write trade log CSV: %s", e)


def compute_stats_from_csv() -> dict:
    """Compute win rate, avg win/loss, expectancy from closed trades in CSV.

    Returns dict with stats, or empty dict if no data.
    """
    if not TRADE_LOG_CSV.exists():
        return {}
    try:
        with open(TRADE_LOG_CSV, newline="") as f:
            rows = list(csv.DictReader(f))

        closed = [r for r in rows if r.get("pnl_dollars")]
        if not closed:
            return {}

        pnls = [float(r["pnl_dollars"]) for r in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate = len(wins) / len(pnls)
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

        return {
            "total_trades": len(pnls),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy": expectancy,
            "total_pnl": sum(pnls),
        }
    except Exception as e:
        logger.warning("Failed to compute stats from CSV: %s", e)
        return {}
