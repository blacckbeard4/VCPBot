"""VCPBot ticker universe — all active, tradable US equities via Alpaca assets API.

Falls back to S&P 500 Wikipedia scrape if Alpaca is unavailable.

Note: SECTOR_MAP and get_sector() were removed — sector lookups are now handled
by scanner._get_sector() which uses a per-process LRU cache via yfinance.
"""

import logging
from typing import Optional

import io
import requests
import pandas as pd

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY

logger = logging.getLogger(__name__)

# Cache so we only fetch once per process lifetime
_universe_cache: Optional[list[str]] = None


def fetch_alpaca_universe() -> list[str]:
    """Fetch all active tradable US equity symbols via Alpaca assets API.

    Filters to NYSE / NASDAQ / ARCA / BATS listed equities (excludes OTC).
    Returns sorted list of ticker strings, or empty list on failure.
    """
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetAssetsRequest
        from alpaca.trading.enums import AssetClass, AssetStatus

        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        assets = client.get_all_assets(
            GetAssetsRequest(
                asset_class=AssetClass.US_EQUITY,
                status=AssetStatus.ACTIVE,
            )
        )

        MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "BATS", "NYSE ARCA"}
        tickers = [
            a.symbol
            for a in assets
            if a.tradable
            and a.fractionable == True  # explicitly require fractional share support
            and getattr(getattr(a, "exchange", None), "value", "") in MAJOR_EXCHANGES
            and "." not in a.symbol  # exclude share classes like BRK.B
        ]
        tickers = sorted(set(tickers))
        logger.info("Fetched %d US equity symbols from Alpaca assets API", len(tickers))
        return tickers

    except Exception as e:
        logger.warning("Alpaca assets fetch failed: %s — falling back to S&P 500", e)
        return []


def fetch_sp500_tickers() -> list[str]:
    """Scrape S&P 500 tickers from Wikipedia (fallback universe)."""
    try:
        url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        headers = {"User-Agent": "Mozilla/5.0 (compatible; VCPBot/1.0)"}
        html = requests.get(url, headers=headers, timeout=15).text
        tables = pd.read_html(io.StringIO(html), match="Symbol")
        df = tables[0]
        tickers: list[str] = []
        for _, row in df.iterrows():
            symbol = str(row["Symbol"]).strip().replace(".", "-")
            tickers.append(symbol)
        result = sorted(set(tickers))
        logger.info("Fetched %d S&P 500 tickers (fallback)", len(result))
        return result
    except Exception as e:
        logger.warning("Wikipedia S&P 500 scrape failed: %s", e)
        return []


def get_full_universe() -> list[str]:
    """Return the full tradable US equity universe (cached per process).

    Tries Alpaca assets API first; falls back to S&P 500 scrape.
    """
    global _universe_cache
    if _universe_cache is not None:
        return _universe_cache

    tickers = fetch_alpaca_universe()
    if not tickers:
        tickers = fetch_sp500_tickers()

    _universe_cache = tickers
    return _universe_cache


def clear_cache() -> None:
    """Clear the universe cache (useful for testing)."""
    global _universe_cache
    _universe_cache = None
