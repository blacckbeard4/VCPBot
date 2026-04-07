"""VCPBot Phase 7 — Trade management and EOD monitoring.

For each open position at market close:
  - If +20% target hit: confirm take-profit (Alpaca handles the limit order)
    and log the win. Mark as TARGET_HIT in DB.
  - If price closed below stop_loss_price: Alpaca's stop order fires automatically.
    Monitor confirms and marks as STOPPED.
  - Logs all trade outcomes to DB and CSV trade log.
  - Sends daily EOD summary via Telegram.

Runs at 4:05 PM EST (after close) and every 30 min intraday.
"""

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from config import TIMEZONE, ACCOUNT_VALUE, TARGET_PCT
import db
import executor as _executor
from notifier import (
    send_stop_alert, send_target_alert, send_eod_summary, send_error_alert,
)

logger = logging.getLogger(__name__)
ET = ZoneInfo(TIMEZONE)


# ─── Helpers ────────────────────────────────────────────────


def _fetch_recent_data(ticker: str, period: str = "1mo") -> Optional[pd.DataFrame]:
    """Fetch recent daily OHLCV for a ticker via yfinance."""
    try:
        df = yf.download(ticker, period=period, interval="1d",
                         progress=False, threads=False, auto_adjust=True)
        if df is not None and len(df) > 0:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df
        return None
    except Exception as e:
        logger.warning("Failed to fetch data for %s: %s", ticker, e)
        return None


def _compute_pnl(entry_price: float, exit_price: float, shares: float) -> float:
    """Compute P&L for a LONG position."""
    return (exit_price - entry_price) * shares


def _compute_r_multiple(
    entry_price: float, exit_price: float, stop_price: float
) -> float:
    """R-multiple = profit per share / risk per share."""
    risk = entry_price - stop_price
    if risk <= 0:
        return 0.0
    profit = exit_price - entry_price
    return round(profit / risk, 2)


def _close_trade(
    trade,
    exit_price: float,
    status: str,
    exit_reason: str,
    account_value: float,
) -> None:
    """Close a trade: compute PnL, update DB, log to CSV, send alert."""
    trade_id = trade["id"]
    ticker = trade["ticker"]
    entry_price = float(trade["entry_price"] or 0)
    stop_price = float(trade["stop_price"] or 0)
    shares = float(trade["shares"] or 0)
    target = float(trade["target_1_price"] or 0)
    pivot = float(trade["pivot_price"] or entry_price)

    pnl = _compute_pnl(entry_price, exit_price, shares)
    r_mult = _compute_r_multiple(entry_price, exit_price, stop_price)
    pnl_pct = (exit_price - entry_price) / entry_price if entry_price else 0
    now = datetime.now(ET).isoformat()
    today = datetime.now(ET).strftime("%Y-%m-%d")

    db.update_trade_status(
        trade_id, status,
        exit_price=exit_price,
        exit_date=now,
        exit_reason=exit_reason,
        pnl=round(pnl, 2),
        r_multiple=r_mult,
    )

    db.log_trade_to_csv(
        ticker=ticker,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target,
        shares=shares,
        account_equity=account_value,
        rs_rank=float(trade["rs_rank"] or 0),
        base_weeks=int(trade["base_duration_weeks"] or 0),
        contraction_depth_pct=float(trade["contraction_depth_pct"] or 0),
        exit_date=today,
        exit_price=exit_price,
        exit_reason=exit_reason,
        pnl_dollars=pnl,
        pnl_pct=pnl_pct,
        r_multiple=r_mult,
    )

    # Recalculate and log expectancy after every close
    stats = db.compute_stats_from_csv()
    if stats:
        logger.info(
            "All-time stats after %s %s: trades=%d win_rate=%.0f%% "
            "avg_win=$%.2f avg_loss=$%.2f expectancy=$%.2f/trade",
            status, ticker,
            stats.get("total_trades", 0),
            stats.get("win_rate", 0) * 100,
            stats.get("avg_win", 0),
            stats.get("avg_loss", 0),
            stats.get("expectancy", 0),
        )
    if status == "STOPPED":
        send_stop_alert(ticker, exit_price, pnl, r_mult, stats=stats)
    elif status == "TARGET_HIT":
        send_target_alert(ticker, exit_price, pnl, r_mult, stats=stats)

    logger.info("%s %s: exit=%.2f pnl=$%.2f r=%.2f", status, ticker, exit_price, pnl, r_mult)


# ─── Per-trade monitor ───────────────────────────────────────


def _monitor_single_trade(trade, account_value: float) -> None:
    """Check a single OPEN trade for target hit or stop hit."""
    ticker = trade["ticker"]
    entry_price = float(trade["entry_price"] or 0)
    stop_price = float(trade["stop_price"] or 0)
    target_price = float(trade["target_1_price"] or 0)

    if entry_price <= 0 or stop_price <= 0:
        return

    df = _fetch_recent_data(ticker, period="5d")
    if df is None or len(df) < 1:
        return

    latest_close = float(df["Close"].iloc[-1])
    latest_low = float(df["Low"].iloc[-1])
    latest_high = float(df["High"].iloc[-1])

    # ── Stop hit: intraday low touched stop ──
    # Note: Alpaca's linked stop order handles actual exit automatically.
    # We detect it here to update DB state and log.
    if latest_low <= stop_price:
        _close_trade(trade, stop_price, "STOPPED", "Stop loss hit", account_value)
        return

    # ── Target hit: +20% limit reached ──
    # Alpaca's take-profit limit order handles actual exit automatically.
    if latest_high >= target_price:
        _close_trade(trade, target_price, "TARGET_HIT", "+20% target hit", account_value)
        return

    logger.debug("%s: close=%.2f stop=%.2f target=%.2f — holding",
                 ticker, latest_close, stop_price, target_price)


# ─── Intraday monitor ────────────────────────────────────────


def run_intraday_monitor() -> None:
    """Poll open positions every 30 min during market hours."""
    open_trades = db.get_open_trades()
    if not open_trades:
        logger.debug("No open trades to monitor")
        return

    account_value = _executor.get_portfolio_value() or ACCOUNT_VALUE
    logger.info("Intraday monitor: %d open positions", len(open_trades))

    for trade in open_trades:
        try:
            _monitor_single_trade(trade, account_value)
        except Exception as e:
            logger.error("Monitor error for %s: %s", trade["ticker"], e)
            db.log_error("monitor", str(type(e).__name__), str(e),
                         ticker=trade["ticker"])


# ─── EOD monitor ─────────────────────────────────────────────


def run_eod_monitor() -> None:
    """4:05 PM routine: check positions, confirm fills, snapshot portfolio.

    Also: cancel any PLACED orders that did not trigger today.
    """
    # 1. Check order fills
    _executor.check_placed_orders()

    # 2. Cancel stale buy-stop orders that didn't trigger (age >= 1 day)
    _executor.cancel_stale_orders(max_days=1)

    # 3. Standard position monitoring pass
    run_intraday_monitor()

    # 4. Compute and snapshot portfolio state
    open_trades = db.get_open_trades()
    account_value = _executor.get_portfolio_value() or ACCOUNT_VALUE

    total_unrealized = 0.0
    for trade in open_trades:
        ticker = trade["ticker"]
        entry_price = float(trade["entry_price"] or 0)
        shares = float(trade["shares"] or 0)
        if entry_price <= 0 or shares <= 0:
            continue
        df = _fetch_recent_data(ticker, period="5d")
        if df is not None and len(df) > 0:
            current_price = float(df["Close"].iloc[-1])
            total_unrealized += _compute_pnl(entry_price, current_price, shares)

    today = datetime.now(ET).strftime("%Y-%m-%d")
    peak = db.get_rolling_peak_account_value(days=60)
    cash = max(0.0, account_value - sum(
        float(t["entry_price"] or 0) * float(t["shares"] or 0)
        for t in open_trades
    ))
    drawdown = (peak - account_value) / peak if peak > 0 else 0.0

    db.insert_portfolio_state(
        date=today,
        account_value=round(account_value, 2),
        cash_available=round(cash, 2),
        open_positions=len(open_trades),
        total_unrealized_pnl=round(total_unrealized, 2),
        peak_account_value=round(max(peak, account_value), 2),
        current_drawdown_pct=round(max(0.0, drawdown), 4),
    )

    # 5. EOD summary
    today_range_start = today
    today_range_end = today
    today_trades = db.get_trades_by_date_range(today_range_start, today_range_end)
    closed_today = [t for t in today_trades
                    if t["status"] in ("STOPPED", "TARGET_HIT")]
    daily_pnl = sum(float(t["pnl"] or 0) for t in closed_today)

    send_eod_summary(
        open_positions=len(open_trades),
        total_unrealized_pnl=total_unrealized,
        trades_closed_today=len(closed_today),
        daily_realized_pnl=daily_pnl,
    )

    logger.info(
        "EOD: %d open, unrealized=%.2f, closed=%d, realized=%.2f, drawdown=%.1f%%",
        len(open_trades), total_unrealized, len(closed_today), daily_pnl, drawdown * 100,
    )
