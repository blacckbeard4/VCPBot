"""VCPBot — Earnings blackout check.

Source priority:
  1. Finnhub earnings calendar API  (reliable, dedicated endpoint)
  2. yfinance .calendar             (fallback, scrapes Yahoo)

Returns 999 (= no blackout) if both sources fail — conservative default
that lets the trade proceed rather than blocking on a data error.
"""

import logging
from datetime import datetime, date
from typing import Optional

import yfinance as yf
import pandas as pd

from config import EARNINGS_BLACKOUT_DAYS, FINNHUB_API_KEY
from finnhub_client import finnhub_next_earnings_days

logger = logging.getLogger(__name__)


# ─── Earnings Blackout ───────────────────────────────────────


def days_to_earnings(ticker: str) -> int:
    """Return the number of calendar days to next earnings.

    Tries Finnhub first, falls back to yfinance.
    Returns 999 if the date is unknown or both sources fail.
    Never raises.
    """
    # ── Primary: Finnhub ──
    if FINNHUB_API_KEY:
        try:
            days = finnhub_next_earnings_days(ticker, FINNHUB_API_KEY)
            if days < 999:
                logger.debug("Finnhub earnings for %s: %d days", ticker, days)
                return days
        except Exception as e:
            logger.debug("Finnhub earnings failed for %s: %s", ticker, e)

    # ── Fallback: yfinance ──
    return _days_to_earnings_yfinance(ticker)


def _days_to_earnings_yfinance(ticker: str) -> int:
    """yfinance earnings date lookup. Returns 999 on any failure."""
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar

        if cal is None:
            return 999

        today = datetime.now().date()

        if isinstance(cal, dict):
            earnings_dates = cal.get("Earnings Date", [])
            if not earnings_dates:
                return 999
            if not hasattr(earnings_dates, "__iter__"):
                earnings_dates = [earnings_dates]
        elif isinstance(cal, pd.DataFrame):
            if "Earnings Date" in cal.columns:
                earnings_dates = cal["Earnings Date"].tolist()
            elif len(cal.columns) > 0:
                earnings_dates = cal.iloc[0].tolist()
            else:
                return 999
        else:
            return 999

        min_days = 999
        for d in earnings_dates:
            try:
                if hasattr(d, "date"):
                    ed = d.date()
                elif isinstance(d, str):
                    ed = datetime.fromisoformat(d[:10]).date()
                elif isinstance(d, date):
                    ed = d
                else:
                    continue
                diff = (ed - today).days
                if diff >= 0:
                    min_days = min(min_days, diff)
            except Exception:
                continue

        return min_days

    except Exception as e:
        logger.debug("yfinance earnings lookup failed for %s: %s", ticker, e)
        return 999


def has_earnings_blackout(ticker: str) -> bool:
    """Return True if next earnings is within EARNINGS_BLACKOUT_DAYS."""
    d = days_to_earnings(ticker)
    if d <= EARNINGS_BLACKOUT_DAYS:
        logger.info("Earnings blackout for %s: %d days to earnings", ticker, d)
        return True
    return False
