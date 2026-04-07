"""VCPBot Telegram notification helper.

All functions are fire-and-forget — they log warnings on failure but NEVER raise.
"""

import logging
from typing import Optional

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_alert(message: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message. Returns True if successful."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return False
    try:
        url = _TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": parse_mode,
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        logger.warning("Telegram API returned %d: %s", resp.status_code, resp.text)
        return False
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)
        return False


# ── Regime / Mode Alerts ────────────────────────────────────


def send_cash_mode_alert(spy_close: float, spy_sma200: float) -> bool:
    """Alert: market entered Cash Mode (SPY below SMA200)."""
    msg = (
        f"\U0001f7e5 <b>CASH MODE ACTIVATED</b>\n"
        f"SPY ${spy_close:.2f} crossed below SMA200 ${spy_sma200:.2f}\n"
        f"No new entries until SPY reclaims SMA200."
    )
    return send_alert(msg)


def send_cash_mode_exit_alert(spy_close: float, spy_sma200: float) -> bool:
    """Alert: SPY reclaimed SMA200, resuming normal scan."""
    msg = (
        f"\U0001f7e2 <b>CASH MODE DEACTIVATED</b>\n"
        f"SPY ${spy_close:.2f} reclaimed SMA200 ${spy_sma200:.2f}\n"
        f"Resuming full scan with 2% risk per trade."
    )
    return send_alert(msg)


def send_ftd_alert(index: str, gain_pct: float, day_number: int) -> bool:
    """Alert: Follow-Through Day detected."""
    msg = (
        f"\U0001f4f0 <b>FOLLOW-THROUGH DAY</b>\n"
        f"{index} up {gain_pct:.1%} on higher volume (Day {day_number} of rally)\n"
        f"Switching to FTD mode — risk reduced to 1% per trade."
    )
    return send_alert(msg)


# ── Signal Alerts ────────────────────────────────────────────


def send_vcp_signal_alert(
    ticker: str,
    pivot: float,
    stop: float,
    rs_rank: float,
    base_weeks: int,
    contractions: list[float],
) -> bool:
    """Alert: new VCP setup queued for next-morning execution."""
    depth_str = " → ".join(f"{d:.1%}" for d in contractions)
    msg = (
        f"\U0001f4cc <b>VCP SETUP: {ticker}</b>\n"
        f"Pivot: ${pivot:.2f} | Stop: ${stop:.2f} | RS Rank: {rs_rank:.0f}\n"
        f"Base: {base_weeks}w | Contractions: {depth_str}\n"
        f"Order queued for next morning open."
    )
    return send_alert(msg)


def send_trade_alert(
    ticker: str,
    shares: float,
    pivot: float,
    stop: float,
    target: float,
    risk_pct: float,
) -> bool:
    """Alert: buy stop limit order placed."""
    msg = (
        f"\U0001f4c8 <b>ORDER PLACED: {ticker} LONG {shares}sh</b>\n"
        f"Buy stop: ${pivot + 0.05:.2f} | Stop loss: ${stop:.2f} | Target: ${target:.2f}\n"
        f"Risk: {risk_pct:.1%} of account"
    )
    return send_alert(msg)


def send_fill_alert(
    ticker: str,
    shares: float,
    fill_price: float,
    stop: float,
    target: float,
) -> bool:
    """Alert: order filled — position is now open."""
    msg = (
        f"\u2705 <b>FILLED: {ticker} {shares}sh @ ${fill_price:.2f}</b>\n"
        f"Stop: ${stop:.2f} | Target: ${target:.2f}"
    )
    return send_alert(msg)


def send_stop_alert(
    ticker: str, exit_price: float, pnl: float, r_multiple: float,
    stats: Optional[dict] = None,
) -> bool:
    """Alert: stop loss triggered, with optional all-time expectancy block."""
    msg = (
        f"\U0001f534 <b>STOPPED OUT: {ticker} @ ${exit_price:.2f}</b>\n"
        f"P&L: ${pnl:+.2f} ({r_multiple:+.2f}R)"
    )
    if stats:
        msg += (
            f"\n\n<b>All-Time Stats</b>\n"
            f"Trades: {stats.get('total_trades', 0)} "
            f"({stats.get('wins', 0)}W/{stats.get('losses', 0)}L) | "
            f"Win rate: {stats.get('win_rate', 0):.0%}\n"
            f"Avg win: ${stats.get('avg_win', 0):+.2f} | Avg loss: ${stats.get('avg_loss', 0):+.2f}\n"
            f"Expectancy: ${stats.get('expectancy', 0):+.2f}/trade"
        )
    return send_alert(msg)


def send_target_alert(
    ticker: str, exit_price: float, pnl: float, r_multiple: float,
    stats: Optional[dict] = None,
) -> bool:
    """Alert: +20% target hit, with optional all-time expectancy block."""
    msg = (
        f"\U0001f7e1 <b>TARGET HIT: {ticker} @ ${exit_price:.2f}</b>\n"
        f"P&L: ${pnl:+.2f} ({r_multiple:+.2f}R) | +20% take profit triggered"
    )
    if stats:
        msg += (
            f"\n\n<b>All-Time Stats</b>\n"
            f"Trades: {stats.get('total_trades', 0)} "
            f"({stats.get('wins', 0)}W/{stats.get('losses', 0)}L) | "
            f"Win rate: {stats.get('win_rate', 0):.0%}\n"
            f"Avg win: ${stats.get('avg_win', 0):+.2f} | Avg loss: ${stats.get('avg_loss', 0):+.2f}\n"
            f"Expectancy: ${stats.get('expectancy', 0):+.2f}/trade"
        )
    return send_alert(msg)


def send_gap_cancel_alert(ticker: str, pivot: float, current_price: float) -> bool:
    """Alert: order cancelled because price gapped too far above pivot."""
    gap_pct = (current_price - pivot) / pivot * 100
    msg = (
        f"\u26a0\ufe0f <b>ORDER CANCELLED: {ticker}</b>\n"
        f"Pivot: ${pivot:.2f} | Current: ${current_price:.2f} (gap {gap_pct:+.1f}%)\n"
        f"Price gapped too far — setup invalidated."
    )
    return send_alert(msg)


def send_error_alert(step: str, error: str) -> bool:
    """Alert: pipeline error."""
    msg = f"\U0001f6a8 <b>ERROR [{step}]:</b>\n{error}"
    return send_alert(msg)


# ── Summary Alerts ───────────────────────────────────────────


def send_pipeline_summary(
    cash_mode: bool,
    ftd_mode: bool,
    tickers_scanned: int,
    tickers_phase2: int,
    tickers_phase3: int,
    vcp_setups: int,
    orders_queued: int,
) -> bool:
    """Send end-of-scan summary."""
    mode = "CASH MODE" if cash_mode else ("FTD MODE" if ftd_mode else "NORMAL")
    msg = (
        f"\U0001f4ca <b>VCP Scan Complete — {mode}</b>\n"
        f"Scanned: {tickers_scanned} → Phase2: {tickers_phase2} → Phase3: {tickers_phase3}\n"
        f"VCP setups found: {vcp_setups} | Orders queued: {orders_queued}"
    )
    return send_alert(msg)


def send_eod_summary(
    open_positions: int,
    total_unrealized_pnl: float,
    trades_closed_today: int,
    daily_realized_pnl: float,
) -> bool:
    """Send end-of-day summary."""
    msg = (
        f"\U0001f4c5 <b>EOD Summary</b>\n"
        f"Open positions: {open_positions}\n"
        f"Unrealized P&L: ${total_unrealized_pnl:+.2f}\n"
        f"Closed today: {trades_closed_today} | Realized: ${daily_realized_pnl:+.2f}"
    )
    return send_alert(msg)


def send_weekly_report(report_text: str) -> bool:
    """Send weekly P&L summary."""
    return send_alert(f"\U0001f4c8 <b>Weekly Report</b>\n{report_text}")
