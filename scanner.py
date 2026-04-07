"""VCPBot Phase 2 + Phase 3 — Universe screener + Trend Template + RS Rank.

Phase 2 hard gates (all must pass):
  1. 50-day avg daily volume > 1,000,000
  2. Close price > $10
  3. Close > 200-day SMA

Phase 3 Trend Template (all 7 must be true):
  1. Close > 150_SMA AND Close > 200_SMA
  2. 150_SMA > 200_SMA
  3. 200_SMA slope > 0 over last 30 calendar days
  4. 50_SMA > 150_SMA AND 50_SMA > 200_SMA
  5. Close > 50_SMA
  6. Close >= 52w_low * 1.30 (at least 30% above 52-week low)
  7. Close >= 52w_high * 0.75 (within 25% of 52-week high)

RS Rank: percentile-ranked across full screened universe.
  RS_Raw = 0.4*ROC_63 + 0.2*ROC_126 + 0.2*ROC_189 + 0.2*ROC_252
  Reject if RS_Rank < 80 (bottom 80% excluded).
"""

import bisect
import gc
import logging
import time
from functools import lru_cache
from typing import Optional

import pandas as pd
import yfinance as yf

from config import (
    SCANNER_BATCH_SIZE, YFINANCE_RETRIES, YFINANCE_RETRY_SLEEP,
    MIN_AVG_VOLUME, MIN_PRICE, RS_MIN_RANK,
    MIN_PCT_ABOVE_52W_LOW, MAX_PCT_BELOW_52W_HIGH, MAX_SECTOR_POSITIONS,
)

logger = logging.getLogger(__name__)


# ─── Sector lookup (cached per process) ─────────────────────


@lru_cache(maxsize=2048)
def _get_sector(ticker: str) -> str:
    """Return the sector for a ticker via yfinance .info. Cached to avoid repeat calls.

    Returns 'Unknown' if the sector cannot be determined.
    """
    try:
        info = yf.Ticker(ticker).info
        sector = info.get("sector") or info.get("sectorDisp") or "Unknown"
        return str(sector)
    except Exception:
        return "Unknown"


# ─── Sector concentration filter ────────────────────────────


def _apply_sector_filter(
    candidates: list[dict],
    open_positions: list,
) -> list[dict]:
    """Reject candidates from sectors already at MAX_SECTOR_POSITIONS open positions.

    Args:
        candidates: tickers that passed RS rank filter.
        open_positions: list of sqlite3.Row trade objects with status='OPEN'.

    Returns subset of candidates that pass the sector cap.
    """
    # Build current sector counts from open positions
    sector_counts: dict[str, int] = {}
    for pos in open_positions:
        sector = _get_sector(str(pos["ticker"]))
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

    approved: list[dict] = []
    for candidate in candidates:
        ticker = candidate["ticker"]
        sector = _get_sector(ticker)

        count = sector_counts.get(sector, 0)
        if count >= MAX_SECTOR_POSITIONS:
            logger.info(
                "SECTOR CAP REJECT: %s — sector '%s' already has %d/%d open position(s)",
                ticker, sector, count, MAX_SECTOR_POSITIONS,
            )
            continue

        # Provisionally increment so subsequent candidates in same sector are also capped
        sector_counts[sector] = count + 1
        approved.append(candidate)

    rejected = len(candidates) - len(approved)
    logger.info(
        "Sector filter: %d passed, %d rejected (MAX_SECTOR_POSITIONS=%d)",
        len(approved), rejected, MAX_SECTOR_POSITIONS,
    )
    return approved

# ─── Indicator helpers ───────────────────────────────────────


def compute_sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(period, min_periods=period).mean()


def compute_roc(close: pd.Series, n: int) -> float:
    """Rate of change over last n bars as a percentage."""
    if len(close) < n + 1:
        return 0.0
    return (float(close.iloc[-1]) - float(close.iloc[-n - 1])) / float(close.iloc[-n - 1]) * 100


def compute_rs_raw(close: pd.Series) -> Optional[float]:
    """Compute Minervini RS_Raw composite score.

    RS_Raw = 0.4*ROC_63 + 0.2*ROC_126 + 0.2*ROC_189 + 0.2*ROC_252
    Returns None if insufficient history.
    """
    if len(close) < 253:
        return None
    roc63 = compute_roc(close, 63)
    roc126 = compute_roc(close, 126)
    roc189 = compute_roc(close, 189)
    roc252 = compute_roc(close, 252)
    return 0.4 * roc63 + 0.2 * roc126 + 0.2 * roc189 + 0.2 * roc252


# ─── Batch downloader ────────────────────────────────────────


def download_batch(
    tickers: list[str],
    period: str = "2y",
    interval: str = "1d",
) -> dict[str, pd.DataFrame]:
    """Download OHLCV for a batch of tickers with retry logic.

    Returns dict mapping ticker → DataFrame. Tickers with < 200 bars are omitted.
    """
    for attempt in range(1, YFINANCE_RETRIES + 1):
        try:
            data = yf.download(
                tickers, period=period, interval=interval,
                group_by="ticker", threads=False, progress=False, auto_adjust=True,
            )
            if data is None or data.empty:
                logger.warning("Batch download empty (attempt %d/%d)",
                               attempt, YFINANCE_RETRIES)
                if attempt < YFINANCE_RETRIES:
                    time.sleep(YFINANCE_RETRY_SLEEP)
                continue

            result: dict[str, pd.DataFrame] = {}
            if len(tickers) == 1:
                t = tickers[0]
                if isinstance(data.columns, pd.MultiIndex):
                    data.columns = data.columns.get_level_values(0)
                if len(data) >= 200:
                    result[t] = data
            else:
                for t in tickers:
                    try:
                        if isinstance(data.columns, pd.MultiIndex):
                            if t not in data.columns.get_level_values(0):
                                continue
                            df = data[t].dropna(how="all")
                        else:
                            df = data
                        if isinstance(df.columns, pd.MultiIndex):
                            df.columns = df.columns.get_level_values(1)
                        if len(df) >= 200:
                            result[t] = df
                    except (KeyError, TypeError):
                        continue
            return result

        except Exception as e:
            logger.warning("Batch download failed (attempt %d/%d): %s",
                           attempt, YFINANCE_RETRIES, e)
            if attempt < YFINANCE_RETRIES:
                time.sleep(YFINANCE_RETRY_SLEEP)

    logger.error("Batch download failed after all retries (%d tickers)", len(tickers))
    return {}


# ─── Phase 2: Hard gate filters ─────────────────────────────


def _passes_phase2(df: pd.DataFrame) -> bool:
    """Apply Phase 2 hard gates. Returns True if stock passes all three."""
    close = df["Close"]
    volume = df["Volume"]

    latest_close = float(close.iloc[-1])

    # Gate 1: price > $10
    if latest_close < MIN_PRICE:
        return False

    # Gate 2: 50-day avg volume > 1M
    adv50 = float(volume.iloc[-50:].mean()) if len(volume) >= 50 else float(volume.mean())
    if adv50 < MIN_AVG_VOLUME:
        return False

    # Gate 3: close > 200-day SMA
    sma200 = compute_sma(close, 200)
    if pd.isna(sma200.iloc[-1]):
        return False
    if latest_close < float(sma200.iloc[-1]):
        return False

    return True


# ─── Phase 3: Trend template ────────────────────────────────


def _apply_trend_template(df: pd.DataFrame) -> Optional[dict]:
    """Apply all 7 trend template checks.

    Returns indicator dict if all pass, None if any fail.
    """
    close = df["Close"]
    high = df["High"]
    low = df["Low"]

    if len(close) < 252:
        return None

    sma50 = compute_sma(close, 50)
    sma150 = compute_sma(close, 150)
    sma200 = compute_sma(close, 200)

    c = float(close.iloc[-1])
    s50 = float(sma50.iloc[-1])
    s150 = float(sma150.iloc[-1])
    s200 = float(sma200.iloc[-1])
    s200_30d_ago = float(sma200.iloc[-31]) if len(sma200) > 31 and not pd.isna(sma200.iloc[-31]) else None

    if any(pd.isna(x) for x in [s50, s150, s200]):
        return None

    # Check 1: Close > 150 SMA AND Close > 200 SMA
    if not (c > s150 and c > s200):
        return None

    # Check 2: 150 SMA > 200 SMA
    if s150 <= s200:
        return None

    # Check 3: 200 SMA slope positive over last 30 days
    if s200_30d_ago is None or s200 <= s200_30d_ago:
        return None

    # Check 4: 50 SMA > 150 SMA AND 50 SMA > 200 SMA
    if not (s50 > s150 and s50 > s200):
        return None

    # Check 5: Close > 50 SMA
    if c <= s50:
        return None

    # Check 6: Close >= 52-week low * 1.30
    w52_low = float(low.iloc[-252:].min())
    if c < w52_low * (1 + MIN_PCT_ABOVE_52W_LOW):
        return None

    # Check 7: Close >= 52-week high * 0.75
    w52_high = float(high.iloc[-252:].max())
    if c < w52_high * (1 - MAX_PCT_BELOW_52W_HIGH):
        return None

    return {
        "close": round(c, 2),
        "sma50": round(s50, 2),
        "sma150": round(s150, 2),
        "sma200": round(s200, 2),
        "w52_low": round(w52_low, 2),
        "w52_high": round(w52_high, 2),
        "adv50": float(df["Volume"].iloc[-50:].mean()),
    }


# ─── Main scanner ────────────────────────────────────────────


def scan_universe(tickers: list[str]) -> list[dict]:
    """Scan all tickers in batches. Apply Phase 2 + Phase 3 filters, then RS rank.

    Returns list of dicts for tickers passing all filters, sorted by RS_Rank desc.

    Each dict contains:
      ticker, close, sma50/150/200, w52_high/low, rs_raw, rs_rank, adv50, df (OHLCV)
    """
    logger.info("Scanner starting — %d tickers in universe", len(tickers))

    phase2_results: list[dict] = []  # {ticker, df, indicators}
    batches = [
        tickers[i:i + SCANNER_BATCH_SIZE]
        for i in range(0, len(tickers), SCANNER_BATCH_SIZE)
    ]

    for batch_idx, batch in enumerate(batches):
        logger.info("Scanning batch %d/%d (%d tickers)",
                    batch_idx + 1, len(batches), len(batch))

        batch_data = download_batch(batch)
        if not batch_data:
            logger.warning("Batch %d returned no data", batch_idx + 1)
            continue

        for ticker, df in batch_data.items():
            try:
                # Phase 2 gate
                if not _passes_phase2(df):
                    continue

                # Phase 3 trend template
                indicators = _apply_trend_template(df)
                if indicators is None:
                    continue

                # Compute RS_Raw (for ranking later)
                rs_raw = compute_rs_raw(df["Close"])
                if rs_raw is None:
                    continue

                phase2_results.append({
                    "ticker": ticker,
                    "df": df,
                    "indicators": indicators,
                    "rs_raw": rs_raw,
                })

            except Exception as e:
                logger.debug("Filter error for %s: %s", ticker, e)

        del batch_data
        gc.collect()

        if batch_idx < len(batches) - 1:
            time.sleep(2)

    logger.info("Phase 2+3: %d tickers passed out of %d scanned",
                len(phase2_results), len(tickers))

    if not phase2_results:
        return []

    # ── RS Rank: percentile rank across all passing stocks ──
    rs_raws = [r["rs_raw"] for r in phase2_results]
    sorted_scores = sorted(rs_raws)
    n = len(sorted_scores)

    def percentile_rank(score: float) -> float:
        # Use bisect for correct tie-handling: ties get the rank of their first occurrence
        rank = bisect.bisect_left(sorted_scores, score)
        return round((rank / (n - 1)) * 98 + 1, 1) if n > 1 else 50.0

    final: list[dict] = []
    for r in phase2_results:
        rs_rank = percentile_rank(r["rs_raw"])
        if rs_rank < RS_MIN_RANK:
            continue
        ind = r["indicators"]
        final.append({
            "ticker": r["ticker"],
            "df": r["df"],
            "close": ind["close"],
            "sma50": ind["sma50"],
            "sma150": ind["sma150"],
            "sma200": ind["sma200"],
            "w52_low": ind["w52_low"],
            "w52_high": ind["w52_high"],
            "adv50": ind["adv50"],
            "rs_raw": round(r["rs_raw"], 2),
            "rs_rank": rs_rank,
        })

    final.sort(key=lambda x: x["rs_rank"], reverse=True)
    logger.info("RS filter: %d tickers with RS_Rank >= %d", len(final), RS_MIN_RANK)

    # ── Sector concentration filter ──
    import db as _db  # local import to avoid circular dependency
    open_positions = _db.get_open_trades()
    final = _apply_sector_filter(final, open_positions)

    return final
