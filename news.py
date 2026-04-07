"""VCPBot — Earnings blackout check.

Uses yfinance calendar (free, no additional API needed).
Rejects stocks with earnings within EARNINGS_BLACKOUT_DAYS calendar days.

Note: The Alpaca News keyword scan was removed — it was never called in the pipeline.
Earnings blackout is the only active gate from this module.
"""

import logging
from datetime import datetime, date
from typing import Optional

import yfinance as yf
import pandas as pd

from config import EARNINGS_BLACKOUT_DAYS

logger = logging.getLogger(__name__)


# ─── Earnings Blackout ───────────────────────────────────────


def days_to_earnings(ticker: str) -> int:
    """Return the number of calendar days to next earnings.

    Returns 999 if the date is unknown or if parsing fails.
    Never raises.
    """
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar

        if cal is None:
            return 999

        today = datetime.now().date()

        # yfinance >= 0.2.x returns a dict or DataFrame
        if isinstance(cal, dict):
            earnings_dates = cal.get("Earnings Date", [])
            if not earnings_dates:
                return 999
            # May be a single value or list
            if not hasattr(earnings_dates, "__iter__"):
                earnings_dates = [earnings_dates]
        elif isinstance(cal, pd.DataFrame):
            # Older yfinance returns a DataFrame
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
        logger.debug("Earnings lookup failed for %s: %s", ticker, e)
        return 999


def has_earnings_blackout(ticker: str) -> bool:
    """Return True if next earnings is within EARNINGS_BLACKOUT_DAYS."""
    d = days_to_earnings(ticker)
    if d <= EARNINGS_BLACKOUT_DAYS:
        logger.info("Earnings blackout for %s: %d days to earnings", ticker, d)
        return True
    return False
