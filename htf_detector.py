"""VCPBot Phase 4b — High Tight Flag (HTF) detection.

Runs in parallel with vcp_detector.py on the same Phase 3 filtered watchlist.
Only tickers that failed VCP solely due to "too few contractions (1)" are
explicitly passed here; the rest of the watchlist is scanned normally.

HTF criteria (ALL must pass):
  1. Prior surge: +100%+ gain in preceding 8 weeks (40 trading days)
  2. Single tight consolidation: current close is < 20% below the recent high
     (measured over the last 15 bars)
  3. Consolidation duration: 5–15 trading days since the recent high
  4. Volume dry-up: avg volume during consolidation < 50-day avg volume

HTF setups share the same execution path as VCP:
  - Same bracket order structure
  - Same 2% risk per trade
  - Same +20% take-profit target
  - Same 7% max stop check (stop = lowest low of consolidation window)
  - Tagged as pattern_type = 'HTF' in the trades table
"""

import logging
from typing import Optional

import pandas as pd

from config import (
    HTF_GAIN_8W_MIN_PCT,
    HTF_CONSOLIDATION_MAX_PCT,
    HTF_DAYS_MIN,
    HTF_DAYS_MAX,
    MAX_STOP_PCT,
    BUY_STOP_OFFSET,
    TARGET_PCT,
)

logger = logging.getLogger(__name__)


def detect_htf(ticker: str, df: pd.DataFrame) -> tuple[Optional[dict], str]:
    """Detect a High Tight Flag setup.

    Args:
        ticker: Stock symbol.
        df: Daily OHLCV DataFrame with DatetimeIndex. Minimum 60 bars.

    Returns (setup_dict, "") on success, (None, reason_str) on rejection.

    Setup dict keys:
        ticker, pivot_price, stop_loss_price,
        gain_8w_pct, consolidation_depth_pct, days_consolidating,
        volume_dry_up, stop_pct_from_pivot, pattern_type
    """
    if len(df) < 60:
        return None, "insufficient history (<60 bars)"

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]

    # ── 1. Prior surge: +100% in preceding 40 bars ──
    if len(close) < 41:
        return None, "insufficient history for 8-week gain calc"

    price_40_bars_ago = float(close.iloc[-41])
    price_today       = float(close.iloc[-1])
    if price_40_bars_ago <= 0:
        return None, "invalid price 40 bars ago"

    gain_8w_pct = (price_today - price_40_bars_ago) / price_40_bars_ago * 100
    if gain_8w_pct < HTF_GAIN_8W_MIN_PCT:
        return None, (
            f"prior surge {gain_8w_pct:.0f}% < {HTF_GAIN_8W_MIN_PCT:.0f}% "
            f"required in 8 weeks"
        )

    # ── 2 + 3. Consolidation: find highest high in last 15 bars ──
    lookback = min(15, len(high))
    recent_window_high = df["High"].iloc[-lookback:]
    recent_high_val    = float(recent_window_high.max())
    recent_high_idx    = int(recent_window_high.values.argmax())  # index within window

    # days_since_high = bars from the high to today (0 = today is the high)
    days_consolidating = lookback - 1 - recent_high_idx

    if not (HTF_DAYS_MIN <= days_consolidating <= HTF_DAYS_MAX):
        return None, (
            f"consolidation {days_consolidating}d outside "
            f"[{HTF_DAYS_MIN}–{HTF_DAYS_MAX}d] window"
        )

    consolidation_depth_pct = (recent_high_val - price_today) / recent_high_val * 100
    if consolidation_depth_pct > HTF_CONSOLIDATION_MAX_PCT:
        return None, (
            f"consolidation depth {consolidation_depth_pct:.1f}% > "
            f"{HTF_CONSOLIDATION_MAX_PCT:.0f}% max"
        )

    # ── 4. Volume dry-up during consolidation ──
    consol_start_idx = len(df) - lookback + recent_high_idx
    consol_volume    = volume.iloc[consol_start_idx:]
    avg_vol_50d      = (
        float(volume.iloc[-50:].mean())
        if len(volume) >= 50 else float(volume.mean())
    )
    consol_avg_vol   = float(consol_volume.mean()) if len(consol_volume) > 0 else avg_vol_50d

    if consol_avg_vol >= avg_vol_50d:
        return None, (
            f"no volume dry-up in consolidation "
            f"(avg {consol_avg_vol:,.0f} >= 50d avg {avg_vol_50d:,.0f})"
        )

    # ── Pivot + stop ──
    pivot_price     = recent_high_val
    stop_loss_price = float(low.iloc[consol_start_idx:].min())

    stop_pct = (pivot_price - stop_loss_price) / pivot_price
    if stop_pct > MAX_STOP_PCT:
        return None, (
            f"stop {stop_pct:.1%} below pivot (max {MAX_STOP_PCT:.0%})"
        )

    logger.info(
        "HTF ✓ %s | pivot=%.2f stop=%.2f | surge=+%.0f%% | "
        "consol=%.1f%% / %dd",
        ticker, pivot_price, stop_loss_price,
        gain_8w_pct, consolidation_depth_pct, days_consolidating,
    )

    return {
        "ticker":                  ticker,
        "pivot_price":             round(pivot_price, 2),
        "stop_loss_price":         round(stop_loss_price, 2),
        "gain_8w_pct":             round(gain_8w_pct, 1),
        "consolidation_depth_pct": round(consolidation_depth_pct, 2),
        "days_consolidating":      days_consolidating,
        "volume_dry_up":           True,
        "stop_pct_from_pivot":     round(stop_pct, 4),
        "pattern_type":            "HTF",
        # Mirror VCP fields expected downstream
        "base_duration_days":      days_consolidating,
        "base_duration_weeks":     max(1, days_consolidating // 5),
        "contraction_depths":      [round(consolidation_depth_pct / 100, 4)],
        "final_contraction_depth": round(consolidation_depth_pct / 100, 4),
        "n_contractions":          1,
        "lsh_price":               round(pivot_price, 2),
        "lsh_date":                "",
    }, ""


def detect_htf_batch(
    scan_results: list[dict],
    htf_candidate_tickers: list[str],
) -> tuple[list[dict], list[dict]]:
    """Run HTF detection on the watchlist.

    All tickers in scan_results are scanned. Tickers in htf_candidate_tickers
    (VCP single-contraction failures) are explicitly included; others are also
    scanned because a stock can qualify as HTF without ever hitting VCP filters.

    Returns (htf_setups, rejections).
    """
    htf_setups: list[dict] = []
    rejections: list[dict] = []
    candidate_set = set(htf_candidate_tickers)

    for stock in scan_results:
        ticker = stock["ticker"]
        df     = stock.get("df")
        if df is None:
            continue

        try:
            htf, reason = detect_htf(ticker, df)
        except Exception as e:
            logger.warning("HTF detection error %s: %s", ticker, e)
            rejections.append({"ticker": ticker, "phase": "HTF",
                                "reason": f"exception: {e}"})
            continue

        if htf is None:
            if ticker in candidate_set:
                # Only log rejections for explicitly passed candidates to avoid noise
                rejections.append({"ticker": ticker, "phase": "HTF", "reason": reason})
            continue

        htf_setups.append({
            **htf,
            "rs_rank": stock["rs_rank"],
            "rs_raw":  stock["rs_raw"],
            "close":   stock["close"],
            "sma50":   stock["sma50"],
            "sma150":  stock["sma150"],
            "sma200":  stock["sma200"],
            "adv50":   stock["adv50"],
        })

    htf_setups.sort(key=lambda x: x["rs_rank"], reverse=True)
    logger.info(
        "HTF detector: %d setups from %d candidates (%d were VCP single-contraction)",
        len(htf_setups), len(scan_results), len(candidate_set),
    )
    return htf_setups, rejections
