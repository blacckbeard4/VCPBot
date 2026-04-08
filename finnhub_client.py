"""Multi-source market data client with rate limiting.

OHLCV bars — fallback chain used in regime.py:
  1. yfinance       — primary (called directly in each module)
  2. Alpaca IEX     — fallback 1 (split+dividend adjusted, free with paper account)
  3. Twelve Data    — fallback 2  [8 credits/min free → capped at 7/min]

Earnings + Sector — used by news.py and scanner.py:
  Finnhub free tier supports:  earnings calendar, company profile/sector
  Finnhub free tier does NOT support: stock candle/OHLCV (premium-only, 403)
  Rate cap: 55 calls/min (hard limit is 60)

All DataFrames returned have:
  - columns: Open, High, Low, Close, Volume  (float)
  - index:   DatetimeIndex, tz-naive, ascending

Public API:
  finnhub_next_earnings_days(symbol, api_key)        → int  (999 = unknown)
  finnhub_sector(symbol, api_key)                    → str  ("Unknown" on fail)
  twelvedata_daily_bars(symbol, api_key, days=730)   → Optional[DataFrame]
  test_sources(finnhub_key, twelvedata_key)          → prints live status table

Run `python finnhub_client.py` to test all sources live.
"""

import logging
import time
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_FINNHUB_BASE = "https://finnhub.io/api/v1"
_TWELVEDATA_BASE = "https://api.twelvedata.com"


# ─── Thread-safe sliding-window rate limiter ────────────────


class _RateLimiter:
    """Sliding-window rate limiter. Thread-safe."""

    def __init__(self, max_calls: int, window: float = 60.0):
        self._max = max_calls
        self._window = window
        self._calls: deque = deque()
        self._lock = threading.Lock()

    def wait(self):
        """Block until a call slot is available, then claim it."""
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] >= self._window:
                self._calls.popleft()
            if len(self._calls) >= self._max:
                sleep_for = self._window - (now - self._calls[0]) + 0.05
                if sleep_for > 0:
                    logger.debug("Rate limiter sleeping %.2fs", sleep_for)
                    time.sleep(sleep_for)
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self._window:
                    self._calls.popleft()
            self._calls.append(time.monotonic())


# One limiter instance per provider — shared across all calls in the process
_finnhub_limiter = _RateLimiter(max_calls=55, window=60.0)
_twelvedata_limiter = _RateLimiter(max_calls=7, window=60.0)


# ─── Finnhub (earnings + sector only on free tier) ──────────


def _finnhub_get(endpoint: str, params: dict, api_key: str) -> Optional[dict]:
    """Rate-limited GET to Finnhub. Returns parsed JSON or None."""
    _finnhub_limiter.wait()
    params["token"] = api_key
    try:
        resp = requests.get(f"{_FINNHUB_BASE}/{endpoint}", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("Finnhub %s failed: %s", endpoint, e)
        return None


def finnhub_next_earnings_days(symbol: str, api_key: str) -> int:
    """Return calendar days to next earnings via Finnhub. Returns 999 if unknown."""
    today = datetime.now(timezone.utc).date()
    data = _finnhub_get(
        "calendar/earnings",
        {
            "symbol": symbol,
            "from": today.strftime("%Y-%m-%d"),
            "to": (today + timedelta(days=90)).strftime("%Y-%m-%d"),
        },
        api_key,
    )
    if not data:
        return 999
    entries = data.get("earningsCalendar", [])
    if not entries:
        return 999
    min_days = 999
    for entry in entries:
        try:
            d = datetime.strptime(entry["date"], "%Y-%m-%d").date()
            diff = (d - today).days
            if diff >= 0:
                min_days = min(min_days, diff)
        except Exception:
            continue
    return min_days


def finnhub_sector(symbol: str, api_key: str) -> str:
    """Return industry sector for symbol via Finnhub. Returns 'Unknown' on failure."""
    data = _finnhub_get("stock/profile2", {"symbol": symbol}, api_key)
    if not data:
        return "Unknown"
    sector = data.get("finnhubIndustry") or data.get("ggroup") or "Unknown"
    return str(sector) if sector else "Unknown"


# ─── Twelve Data (OHLCV bars, free tier) ─────────────────────


def twelvedata_daily_bars(symbol: str, api_key: str, days: int = 730) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV from Twelve Data. Returns None on failure.

    Returns split+dividend adjusted prices.
    Free tier: 800 credits/day, 8/min — capped at 7 via _twelvedata_limiter.
    """
    _twelvedata_limiter.wait()
    params = {
        "symbol": symbol,
        "interval": "1day",
        "outputsize": min(days, 5000),
        "apikey": api_key,
        "format": "JSON",
        "order": "ASC",
    }
    try:
        resp = requests.get(f"{_TWELVEDATA_BASE}/time_series", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Twelve Data request failed for %s: %s", symbol, e)
        return None

    if data.get("status") == "error" or "values" not in data:
        logger.warning("Twelve Data: no values for %s (%s)", symbol,
                       data.get("message", "unknown error"))
        return None
    try:
        rows = data["values"]
        df = pd.DataFrame({
            "Open":   [float(r["open"])   for r in rows],
            "High":   [float(r["high"])   for r in rows],
            "Low":    [float(r["low"])    for r in rows],
            "Close":  [float(r["close"])  for r in rows],
            "Volume": [float(r["volume"]) for r in rows],
        }, index=pd.to_datetime([r["datetime"] for r in rows]))
        df.index.name = "Date"
        df.sort_index(inplace=True)
        logger.info("Twelve Data: %d bars for %s", len(df), symbol)
        return df
    except Exception as e:
        logger.warning("Twelve Data parse error for %s: %s", symbol, e)
        return None


# ─── Live source test ────────────────────────────────────────


def test_sources(finnhub_key: str, twelvedata_key: str, symbol: str = "SPY") -> None:
    """Test all data sources live and print a status table.

    Run with:  python finnhub_client.py
    """
    import yfinance as yf

    print(f"\n{'='*62}")
    print(f"  Data source health check — {symbol}  ({datetime.now().strftime('%Y-%m-%d %H:%M')})")
    print(f"{'='*62}")

    results = []

    # 1. yfinance
    try:
        df = yf.download(symbol, period="5d", interval="1d",
                         progress=False, threads=False, auto_adjust=True)
        if df is not None and len(df) >= 1:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            results.append(("yfinance (OHLCV)",      "✅ OK",   f"{len(df)} bars, last close={float(df['Close'].iloc[-1]):.2f}"))
        else:
            results.append(("yfinance (OHLCV)",      "❌ FAIL", "empty response"))
    except Exception as e:
        results.append(("yfinance (OHLCV)",           "❌ FAIL", str(e)[:60]))

    # 2. Alpaca IEX daily bars
    try:
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req = StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame.Day,
            start=datetime.now(timezone.utc) - timedelta(days=10),
            end=datetime.now(timezone.utc), feed="iex", adjustment="all",
        )
        bars = client.get_stock_bars(req)
        try:
            bars_list = bars[symbol]
        except Exception:
            bars_list = None
        if bars_list:
            results.append(("Alpaca IEX (OHLCV)",    "✅ OK",   f"{len(bars_list)} bars, last close={float(bars_list[-1].close):.2f}"))
        else:
            results.append(("Alpaca IEX (OHLCV)",    "❌ FAIL", "empty response"))
    except Exception as e:
        results.append(("Alpaca IEX (OHLCV)",         "❌ FAIL", str(e)[:60]))

    # 3. Twelve Data daily bars
    try:
        df = twelvedata_daily_bars(symbol, twelvedata_key, days=10)
        if df is not None and len(df) >= 1:
            results.append(("Twelve Data (OHLCV)",   "✅ OK",   f"{len(df)} bars, last close={float(df['Close'].iloc[-1]):.2f}"))
        else:
            results.append(("Twelve Data (OHLCV)",   "❌ FAIL", "empty response"))
    except Exception as e:
        results.append(("Twelve Data (OHLCV)",        "❌ FAIL", str(e)[:60]))

    # 4. Finnhub earnings (ETFs return 999 — test with AAPL)
    try:
        days_to_e = finnhub_next_earnings_days("AAPL", finnhub_key)
        results.append(("Finnhub earnings",           "✅ OK",   f"AAPL: next earnings in {days_to_e} days"))
    except Exception as e:
        results.append(("Finnhub earnings",           "❌ FAIL", str(e)[:60]))

    # 5. Finnhub sector
    try:
        sector = finnhub_sector("AAPL", finnhub_key)
        results.append(("Finnhub sector",             "✅ OK",   f"AAPL sector='{sector}'"))
    except Exception as e:
        results.append(("Finnhub sector",             "❌ FAIL", str(e)[:60]))

    # Print table
    print(f"\n{'Source':<26} {'Status':<12} {'Detail'}")
    print("-" * 62)
    for source, status, detail in results:
        print(f"{source:<26} {status:<12} {detail}")
    print()

    ok = sum(1 for _, s, _ in results if "OK" in s)
    print(f"  {ok}/{len(results)} sources healthy")
    print()


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.WARNING)
    fh_key = os.getenv("FINNHUB_API_KEY", "")
    td_key = os.getenv("TWELVE_DATA_API_KEY", "")
    if not fh_key or not td_key:
        print("ERROR: set FINNHUB_API_KEY and TWELVE_DATA_API_KEY in .env")
    else:
        test_sources(fh_key, td_key)
