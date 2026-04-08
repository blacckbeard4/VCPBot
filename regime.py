"""VCPBot Phase 1 — Market regime detection.

Detects:
  - CASH_MODE: SPY < SMA200 → no new entries
  - FTD_MODE: Follow-Through Day fired during Cash Mode → 1% risk
  - NORMAL: SPY >= SMA200 → full 2% risk

FTD logic:
  - Day 1 = first up close after a downtrend (while in Cash Mode)
  - FTD fires on Day 4-7 if index closes up >= 1.5% on higher volume
  - Rally invalidated if index undercuts Day 1 intraday low before FTD fires
  - Distribution day within 3 sessions of FTD reverts to CASH_MODE
  - Distribution check covers BOTH SPY and QQQ

Post-FTD 3-session distribution window:
  - ftd_date is persisted to DB when FTD fires
  - Each subsequent EOD run computes sessions_since_ftd
  - If SPY or QQQ closes down on higher volume within those 3 sessions → CASH
  - After 3 clean sessions the window closes; FTD_MODE stays active

Persists regime state across restarts via SQLite regime_state table.
"""

import logging
import time
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
import pandas_market_calendars as mcal

from config import (
    YFINANCE_RETRIES, YFINANCE_RETRY_SLEEP, TIMEZONE,
    FTD_MIN_DAY, FTD_MAX_DAY, FTD_MIN_GAIN_PCT, FTD_DISTRIBUTION_WINDOW,
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
)
import db

logger = logging.getLogger(__name__)
ET = ZoneInfo(TIMEZONE)
NYSE = mcal.get_calendar("NYSE")


# ─── Trading session counter ─────────────────────────────────


def _trading_sessions_since(from_date_str: Optional[str]) -> int:
    """Return the number of NYSE trading sessions between from_date and today (exclusive of from_date)."""
    if not from_date_str:
        return 999  # treated as "window expired"
    try:
        from_dt = datetime.strptime(from_date_str, "%Y-%m-%d").date()
        today = datetime.now(ET).date()
        if today <= from_dt:
            return 0
        schedule = NYSE.valid_days(start_date=from_dt, end_date=today)
        # exclude from_date itself; count subsequent sessions up to and including today
        return max(0, len(schedule) - 1)
    except Exception:
        return 999


# ─── Public API ──────────────────────────────────────────────


def detect_regime() -> dict:
    """Run regime detection. Returns regime dict and persists state to DB.

    Returns:
        {
            "cash_mode": bool,
            "ftd_mode": bool,
            "regime_label": "NORMAL" | "CASH" | "FTD",
            "spy_close": float,
            "spy_sma200": float,
            "timestamp": str,
        }
    """
    spy_df = _download_index("SPY")
    qqq_df = _download_index("QQQ")
    result = _compute_regime(spy_df, qqq_df)

    # Persist to DB
    db.upsert_regime_state(
        cash_mode=result["cash_mode"],
        ftd_mode=result["ftd_mode"],
        spy_close=result["spy_close"],
        spy_sma200=result["spy_sma200"],
        rally_day1_low=result.get("rally_day1_low"),
        rally_day1_date=result.get("rally_day1_date"),
        rally_day_count=result.get("rally_day_count", 0),
        ftd_date=result.get("ftd_date"),
    )

    logger.info(
        "Regime: %s | SPY=%.2f SMA200=%.2f | cash=%s ftd=%s",
        result["regime_label"], result["spy_close"], result["spy_sma200"],
        result["cash_mode"], result["ftd_mode"],
    )
    return result


def get_spy_data() -> pd.DataFrame:
    """Return SPY 2y daily OHLCV DataFrame (used by scanner for RS calc)."""
    return _download_index("SPY")


# ─── Core logic ──────────────────────────────────────────────


def _compute_regime(spy_df: pd.DataFrame, qqq_df: pd.DataFrame) -> dict:
    """Compute regime from SPY OHLCV data.

    Reads prior regime state from DB and applies FTD / rally logic.
    """
    close = spy_df["Close"]
    volume = spy_df["Volume"]

    sma200 = close.rolling(200).mean()

    latest_close = float(close.iloc[-1])
    latest_sma200 = float(sma200.iloc[-1])

    above_sma200 = latest_close >= latest_sma200

    # Load prior state
    prior = db.get_regime_state()
    was_cash_mode = bool(prior["cash_mode"]) if prior else False
    was_ftd_mode = bool(prior["ftd_mode"]) if prior else False
    rally_day_count: int = int(prior["rally_day_count"] or 0) if prior else 0
    rally_day1_low: Optional[float] = float(prior["rally_day1_low"]) if prior and prior["rally_day1_low"] else None
    rally_day1_date: Optional[str] = prior["rally_day1_date"] if prior else None
    ftd_date: Optional[str] = prior["ftd_date"] if prior else None

    now = datetime.now(ET).isoformat()

    # ── Case 1: SPY above SMA200 → NORMAL ──
    if above_sma200:
        if was_cash_mode or was_ftd_mode:
            logger.info("SPY reclaimed SMA200 — switching to NORMAL mode")
        return {
            "cash_mode": False,
            "ftd_mode": False,
            "regime_label": "NORMAL",
            "spy_close": round(latest_close, 2),
            "spy_sma200": round(latest_sma200, 2),
            "rally_day_count": 0,
            "rally_day1_low": None,
            "rally_day1_date": None,
            "ftd_date": None,
            "timestamp": now,
        }

    # ── SPY is below SMA200 ──
    # Was previously normal → entering Cash Mode
    if not was_cash_mode and not was_ftd_mode:
        logger.info("SPY crossed below SMA200 — entering CASH MODE")
        return {
            "cash_mode": True,
            "ftd_mode": False,
            "regime_label": "CASH",
            "spy_close": round(latest_close, 2),
            "spy_sma200": round(latest_sma200, 2),
            "rally_day_count": 0,
            "rally_day1_low": None,
            "rally_day1_date": None,
            "ftd_date": None,
            "timestamp": now,
        }

    # ── Continuing in CASH or FTD mode — check for distribution / FTD ──
    cash_mode, ftd_mode, rally_day_count, rally_day1_low, rally_day1_date, ftd_date, ftd_gain_pct = _update_ftd_state(
        spy_df=spy_df,
        qqq_df=qqq_df,
        was_cash_mode=was_cash_mode,
        was_ftd_mode=was_ftd_mode,
        rally_day_count=rally_day_count,
        rally_day1_low=rally_day1_low,
        rally_day1_date=rally_day1_date,
        ftd_date=ftd_date,
    )

    label = "CASH" if cash_mode else ("FTD" if ftd_mode else "NORMAL")
    return {
        "cash_mode": cash_mode,
        "ftd_mode": ftd_mode,
        "regime_label": label,
        "spy_close": round(latest_close, 2),
        "spy_sma200": round(latest_sma200, 2),
        "rally_day_count": rally_day_count,
        "rally_day1_low": rally_day1_low,
        "rally_day1_date": rally_day1_date,
        "ftd_date": ftd_date,
        "ftd_gain_pct": ftd_gain_pct,
        "timestamp": now,
    }


def _update_ftd_state(
    spy_df: pd.DataFrame,
    qqq_df: pd.DataFrame,
    was_cash_mode: bool,
    was_ftd_mode: bool,
    rally_day_count: int,
    rally_day1_low: Optional[float],
    rally_day1_date: Optional[str],
    ftd_date: Optional[str],
) -> tuple[bool, bool, int, Optional[float], Optional[str], Optional[str], float]:
    """Apply FTD detection logic.

    Returns (cash_mode, ftd_mode, day_count, day1_low, day1_date, ftd_date, ftd_gain_pct).
    ftd_gain_pct is non-zero only on the session FTD fires.
    """
    spy_close = float(spy_df["Close"].iloc[-1])
    spy_prev_close = float(spy_df["Close"].iloc[-2]) if len(spy_df) >= 2 else spy_close
    # Fix: use actual intraday Low for Day 1 low tracking (not Close as proxy)
    spy_today_low = float(spy_df["Low"].iloc[-1])
    spy_today_vol = float(spy_df["Volume"].iloc[-1])
    spy_prev_vol = float(spy_df["Volume"].iloc[-2]) if len(spy_df["Volume"]) >= 2 else spy_today_vol

    # QQQ distribution data
    qqq_close = float(qqq_df["Close"].iloc[-1]) if qqq_df is not None and len(qqq_df) >= 2 else None
    qqq_prev_close = float(qqq_df["Close"].iloc[-2]) if qqq_df is not None and len(qqq_df) >= 2 else None
    qqq_today_vol = float(qqq_df["Volume"].iloc[-1]) if qqq_df is not None and len(qqq_df) >= 2 else None
    qqq_prev_vol = float(qqq_df["Volume"].iloc[-2]) if qqq_df is not None and len(qqq_df) >= 2 else None

    # ── Post-FTD distribution day window (3 trading sessions) ──
    if was_ftd_mode:
        sessions_since_ftd = _trading_sessions_since(ftd_date)

        if sessions_since_ftd <= FTD_DISTRIBUTION_WINDOW:
            # Check SPY for distribution
            spy_dist = spy_close < spy_prev_close and spy_today_vol > spy_prev_vol
            # Check QQQ for distribution
            qqq_dist = (
                qqq_close is not None
                and qqq_prev_close is not None
                and qqq_today_vol is not None
                and qqq_prev_vol is not None
                and qqq_close < qqq_prev_close
                and qqq_today_vol > qqq_prev_vol
            )

            if spy_dist or qqq_dist:
                which = []
                if spy_dist:
                    which.append("SPY")
                if qqq_dist:
                    which.append("QQQ")
                logger.warning(
                    "Distribution day detected on %s (session %d of %d post-FTD) — reverting to CASH",
                    "+".join(which), sessions_since_ftd, FTD_DISTRIBUTION_WINDOW,
                )
                return True, False, 0, None, None, None, 0.0
            else:
                logger.info(
                    "Post-FTD session %d/%d: no distribution — FTD_MODE continues",
                    sessions_since_ftd, FTD_DISTRIBUTION_WINDOW,
                )
        else:
            logger.info(
                "Post-FTD distribution window closed (%d sessions) — FTD_MODE locked in",
                sessions_since_ftd,
            )

        # Stay in FTD mode
        return False, True, rally_day_count, rally_day1_low, rally_day1_date, ftd_date, 0.0

    # ── Rally attempt tracking (while in Cash Mode) ──
    today_is_up = spy_close > spy_prev_close

    if rally_day_count == 0:
        # Look for Day 1: first up close while in Cash Mode
        if today_is_up and was_cash_mode:
            # Use actual intraday Low for the Day 1 low anchor
            day1_low = spy_today_low
            day1_date = datetime.now(ET).strftime("%Y-%m-%d")
            logger.info("Rally Day 1 started, intraday_low=%.2f", day1_low)
            return True, False, 1, day1_low, day1_date, None, 0.0

    elif rally_day_count >= 1:
        # Check if Day 1 intraday low is undercut → invalidate rally
        if rally_day1_low is not None and spy_today_low < rally_day1_low:
            logger.info(
                "Rally invalidated — SPY intraday low %.2f undercut Day 1 low %.2f — reset to Day 0",
                spy_today_low, rally_day1_low,
            )
            return True, False, 0, None, None, None, 0.0

        new_day_count = rally_day_count + 1

        # Check for FTD on Day 4-7
        if FTD_MIN_DAY <= new_day_count <= FTD_MAX_DAY:
            gain_pct = (spy_close - spy_prev_close) / spy_prev_close
            is_ftd = (
                today_is_up
                and gain_pct >= FTD_MIN_GAIN_PCT
                and spy_today_vol > spy_prev_vol
            )
            if is_ftd:
                fired_date = datetime.now(ET).strftime("%Y-%m-%d")
                logger.info(
                    "FTD fired on Day %d! SPY gain=%.1f%% on higher volume — switching to FTD_MODE",
                    new_day_count, gain_pct * 100,
                )
                return False, True, new_day_count, rally_day1_low, rally_day1_date, fired_date, gain_pct

        # Exceeded day 7 without FTD → rally failed, reset
        if new_day_count > FTD_MAX_DAY:
            logger.info("Rally exceeded day %d without FTD — resetting to Day 0", FTD_MAX_DAY)
            return True, False, 0, None, None, None, 0.0

        return was_cash_mode, was_ftd_mode, new_day_count, rally_day1_low, rally_day1_date, ftd_date, 0.0

    # Default: stay in current state
    return was_cash_mode, was_ftd_mode, rally_day_count, rally_day1_low, rally_day1_date, ftd_date, 0.0


# ─── Index Download ───────────────────────────────────────────


def _download_index(symbol: str) -> pd.DataFrame:
    """Download 2y daily OHLCV for a given index symbol.

    Tries yfinance first (with retries), then falls back to Alpaca's
    historical bars API if yfinance is unavailable.
    """
    # ── Primary: yfinance ──
    for attempt in range(1, YFINANCE_RETRIES + 1):
        try:
            df = yf.download(
                symbol, period="2y", interval="1d",
                progress=False, threads=False, auto_adjust=True,
            )
            if df is not None and len(df) >= 200:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                return df
            logger.warning(
                "%s yfinance returned %d rows (need 200), retry %d/%d",
                symbol, len(df) if df is not None else 0, attempt, YFINANCE_RETRIES,
            )
        except Exception as e:
            logger.warning("%s yfinance failed (attempt %d/%d): %s",
                           symbol, attempt, YFINANCE_RETRIES, e)
        if attempt < YFINANCE_RETRIES:
            time.sleep(YFINANCE_RETRY_SLEEP)

    # ── Fallback: Alpaca historical daily bars ──
    logger.warning("%s: yfinance exhausted all retries — trying Alpaca data API", symbol)
    df = _download_index_alpaca(symbol)
    if df is not None and len(df) >= 200:
        return df

    raise RuntimeError(f"Failed to download {symbol} data after all retries (yfinance + Alpaca)")


def _download_index_alpaca(symbol: str) -> Optional[pd.DataFrame]:
    """Fetch 2y of daily OHLCV bars from Alpaca as a fallback.

    Returns a DataFrame with columns [Open, High, Low, Close, Volume] indexed
    by date (same shape as the yfinance output), or None on failure.
    """
    try:
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=730)  # ~2 years

        request = StockBarsRequest(
            symbol_or_symbols=[symbol],
            timeframe=TimeFrame.Day,
            start=start_dt,
            end=end_dt,
            feed="iex",  # free data feed; falls back gracefully on paper accounts
        )
        bars_response = data_client.get_stock_bars(request)

        if symbol not in bars_response or not bars_response[symbol]:
            logger.warning("Alpaca returned no bars for %s", symbol)
            return None

        rows = [
            {
                "Date": bar.timestamp,
                "Open": float(bar.open),
                "High": float(bar.high),
                "Low": float(bar.low),
                "Close": float(bar.close),
                "Volume": float(bar.volume),
            }
            for bar in bars_response[symbol]
        ]
        df = pd.DataFrame(rows).set_index("Date")
        df.index = pd.to_datetime(df.index, utc=True).tz_convert(None)
        logger.info("Alpaca fallback: fetched %d bars for %s", len(df), symbol)
        return df

    except Exception as e:
        logger.warning("Alpaca fallback failed for %s: %s", symbol, e)
        return None
