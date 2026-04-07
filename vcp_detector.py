"""VCPBot Phase 4 — VCP (Volatility Contraction Pattern) detection.

Checks for each candidate stock:
  1. Base duration >= 4 weeks (not making new 52-week highs recently)
  2. Earnings blackout: next earnings > 14 calendar days away
  3. Contraction sequence: 2-4 successive pullbacks, each shallower than last
  4. Volume dry-up in the final (tightest) contraction
  5. Final contraction depth < 8%
  6. Pivot price = high of the final tight contraction range
  7. Stop loss = low of the final tight contraction (max 7% below pivot)

Returns a VCP setup dict or None if the stock doesn't qualify.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import numpy as np

from config import (
    MIN_BASE_WEEKS, EARNINGS_BLACKOUT_DAYS,
    MAX_FINAL_CONTRACTION_PCT, MIN_CONTRACTIONS, MAX_CONTRACTIONS,
    SWING_PIVOT_BARS, MAX_STOP_PCT,
)
from news import has_earnings_blackout

logger = logging.getLogger(__name__)


# ─── Swing high / low detection ─────────────────────────────


def find_swing_highs(high: pd.Series, n: int = 3) -> list[int]:
    """Return indices of local maxima (swing highs).

    A bar is a swing high if its high is >= the n bars on each side.
    """
    vals = high.values
    indices = []
    for i in range(n, len(vals) - n):
        if all(vals[i] >= vals[i - j] for j in range(1, n + 1)) and \
           all(vals[i] >= vals[i + j] for j in range(1, n + 1)):
            indices.append(i)
    return indices


def find_swing_lows(low: pd.Series, n: int = 3) -> list[int]:
    """Return indices of local minima (swing lows)."""
    vals = low.values
    indices = []
    for i in range(n, len(vals) - n):
        if all(vals[i] <= vals[i - j] for j in range(1, n + 1)) and \
           all(vals[i] <= vals[i + j] for j in range(1, n + 1)):
            indices.append(i)
    return indices


# ─── Contraction builder ─────────────────────────────────────


def _build_contractions(
    high: pd.Series,
    low: pd.Series,
    n: int = SWING_PIVOT_BARS,
) -> list[dict]:
    """Build list of (swing_high → swing_low) contraction pairs.

    Each pair has: sh_idx, sl_idx, sh_price, sl_price, depth (fraction).
    Ordered chronologically.
    """
    sh_indices = find_swing_highs(high, n)
    sl_indices = find_swing_lows(low, n)

    if not sh_indices or not sl_indices:
        return []

    contractions = []
    used_lows: set[int] = set()

    for sh_idx in sh_indices:
        sh_price = float(high.iloc[sh_idx])

        # Find the first swing low that comes AFTER this swing high
        subsequent_lows = [i for i in sl_indices if i > sh_idx and i not in used_lows]
        if not subsequent_lows:
            continue

        sl_idx = subsequent_lows[0]
        sl_price = float(low.iloc[sl_idx])
        depth = (sh_price - sl_price) / sh_price

        contractions.append({
            "sh_idx": sh_idx,
            "sl_idx": sl_idx,
            "sh_price": sh_price,
            "sl_price": sl_price,
            "depth": depth,
        })
        used_lows.add(sl_idx)

    # Sort chronologically by swing high
    contractions.sort(key=lambda x: x["sh_idx"])
    return contractions


# ─── Main VCP detector ───────────────────────────────────────


def detect_vcp(ticker: str, df: pd.DataFrame) -> Optional[dict]:
    """Detect VCP pattern on daily OHLCV data.

    Args:
        ticker: Stock symbol.
        df: Daily OHLCV DataFrame with columns Open, High, Low, Close, Volume.
            Must have a DatetimeIndex. Should be at least 252 bars.

    Returns dict with setup details, or None if not a valid VCP setup.

    Return dict keys:
        ticker, pivot_price, stop_loss_price, target_price,
        base_duration_weeks, contraction_depths (list[float]),
        volume_dry_up (bool), base_start_date (str)
    """
    if len(df) < 252:
        return None

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    volume = df["Volume"]

    # ── 1. Base duration: stock must NOT have made a new 52-week high recently ──
    year_high_price = float(high.iloc[-252:].max())
    # Find the index position of the 52-week high in the last 252 bars
    year_high_loc = int(np.argmax(high.iloc[-252:].values))
    # year_high_loc is 0-indexed from start of the 252-bar window
    # Convert to position from the end of the series
    bars_since_year_high = 252 - year_high_loc - 1

    # Base duration in calendar days (approximate from trading days)
    base_calendar_days = bars_since_year_high * 7 // 5
    base_weeks = base_calendar_days // 7

    if base_weeks < MIN_BASE_WEEKS:
        logger.debug("%s: base too short (%d weeks < %d)", ticker, base_weeks, MIN_BASE_WEEKS)
        return None

    # ── 2. Earnings blackout ──
    if has_earnings_blackout(ticker):
        logger.debug("%s: earnings blackout — skipping", ticker)
        return None

    # ── 3. Work within the base period ──
    # Use bars from the 52-week high to today
    base_start_idx = len(df) - 252 + year_high_loc
    if bars_since_year_high < 20:
        return None  # Too little data in base to detect contractions
    base_df = df.iloc[base_start_idx:]

    base_high = base_df["High"]
    base_low = base_df["Low"]
    base_volume = base_df["Volume"]

    # ── 4. Find contractions ──
    contractions = _build_contractions(base_high, base_low, n=SWING_PIVOT_BARS)

    if len(contractions) < MIN_CONTRACTIONS:
        logger.debug("%s: only %d contractions found (need %d)",
                     ticker, len(contractions), MIN_CONTRACTIONS)
        return None

    # Take the last 2-4 contractions
    recent = contractions[-MAX_CONTRACTIONS:]

    # Must have at least MIN_CONTRACTIONS
    if len(recent) < MIN_CONTRACTIONS:
        return None

    # ── 5. Verify tightening: each depth must be smaller than the prior ──
    depths = [c["depth"] for c in recent]
    for i in range(1, len(depths)):
        if depths[i] >= depths[i - 1]:
            logger.debug("%s: contraction not tightening: %.1f%% >= %.1f%%",
                         ticker, depths[i] * 100, depths[i - 1] * 100)
            return None

    # ── 6. Final contraction must be < 8% ──
    final_depth = depths[-1]
    if final_depth >= MAX_FINAL_CONTRACTION_PCT:
        logger.debug("%s: final contraction %.1f%% >= %.1f%% max",
                     ticker, final_depth * 100, MAX_FINAL_CONTRACTION_PCT * 100)
        return None

    last_contraction = recent[-1]
    sh_idx = last_contraction["sh_idx"]  # index within base_df
    sl_idx = last_contraction["sl_idx"]  # index within base_df

    # ── 7. Volume dry-up in the final contraction ──
    # Compare avg volume during final contraction to 50-day avg volume
    final_range_volume = base_volume.iloc[sh_idx:sl_idx + 1]
    if len(final_range_volume) < 1:
        return None

    avg_vol_50d = float(volume.iloc[-50:].mean()) if len(volume) >= 50 else float(volume.mean())
    final_avg_vol = float(final_range_volume.mean())
    volume_dry_up = final_avg_vol < avg_vol_50d

    if not volume_dry_up:
        logger.debug("%s: volume NOT drying up in final contraction (%.0f vs avg %.0f)",
                     ticker, final_avg_vol, avg_vol_50d)
        return None

    # ── 8. Pivot = high of the tightest contraction range (and beyond to today) ──
    # The pivot is the highest high from the final contraction's swing high forward
    pivot_price = float(base_high.iloc[sh_idx:].max())

    # ── 9. Stop loss = low of the final contraction ──
    stop_loss_price = last_contraction["sl_price"]

    # Hard cap: stop must be <= 7% below pivot
    stop_pct = (pivot_price - stop_loss_price) / pivot_price
    if stop_pct > MAX_STOP_PCT:
        logger.debug("%s: stop %.1f%% below pivot (max %.1f%%)",
                     ticker, stop_pct * 100, MAX_STOP_PCT * 100)
        return None

    # Base start date (approximately when the 52-week high occurred)
    if hasattr(df.index, "strftime"):
        base_start_date = str(df.index[base_start_idx].date())
    else:
        base_start_date = ""

    logger.info(
        "VCP: %s | pivot=%.2f stop=%.2f | base=%dw | contractions=%s",
        ticker, pivot_price, stop_loss_price, base_weeks,
        [f"{d:.1%}" for d in depths],
    )

    return {
        "ticker": ticker,
        "pivot_price": round(pivot_price, 2),
        "stop_loss_price": round(stop_loss_price, 2),
        "base_duration_weeks": base_weeks,
        "contraction_depths": [round(d, 4) for d in depths],
        "final_contraction_depth": round(final_depth, 4),
        "volume_dry_up": volume_dry_up,
        "base_start_date": base_start_date,
        "stop_pct_from_pivot": round(stop_pct, 4),
        "year_high_price": round(year_high_price, 2),
    }


def detect_vcp_batch(scan_results: list[dict]) -> list[dict]:
    """Run VCP detection on all scanner results.

    Args:
        scan_results: Output from scanner.scan_universe().
                      Each dict must have 'ticker', 'df', 'rs_rank'.

    Returns list of dicts combining scan_result fields with VCP detection results.
    """
    vcp_setups: list[dict] = []

    for stock in scan_results:
        ticker = stock["ticker"]
        df = stock.get("df")
        if df is None:
            continue

        try:
            vcp = detect_vcp(ticker, df)
            if vcp is None:
                continue

            # Merge scanner fields with VCP result
            setup = {
                **vcp,
                "rs_rank": stock["rs_rank"],
                "rs_raw": stock["rs_raw"],
                "close": stock["close"],
                "sma50": stock["sma50"],
                "sma150": stock["sma150"],
                "sma200": stock["sma200"],
                "adv50": stock["adv50"],
            }
            vcp_setups.append(setup)

        except Exception as e:
            logger.warning("VCP detection error for %s: %s", ticker, e)

    vcp_setups.sort(key=lambda x: x["rs_rank"], reverse=True)
    logger.info("VCP detector: %d setups found from %d candidates",
                len(vcp_setups), len(scan_results))
    return vcp_setups
