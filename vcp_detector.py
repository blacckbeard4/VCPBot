"""VCPBot Phase 4 — VCP (Volatility Contraction Pattern) detection.

3-tier hierarchical pivot detection:

  Pass 1 — Macro (n=SWING_MACRO_N=15):
    Identifies the Left Side High (LSH) — the most recent major swing high
    that preceded a >=15% pullback. This anchors the base start (T_start).
    Also confirms the prior uptrend (>=30% advance into the LSH).
    Base duration = trading days from T_start to today. Minimum 20 days.

  Pass 2 — Contraction (n=SWING_CONTRACTION_N=8):
    Within the base window (T_start → today), identifies intermediate swing
    highs and lows. Each consecutive high→low pair is a contraction.
    Requires 2–6 contractions each shallower than the prior (tightening).
    Final contraction depth must be < 8%.

  Pass 3 — Micro (n=SWING_MICRO_N=3):
    Within the final contraction window only, identifies the exact pivot
    (highest high) for order placement and stop (lowest low).
    Stop check: (pivot - stop) / pivot <= 7%.

  Volume dry-up: avg volume during final contraction < 50-day avg.
"""

import logging
from typing import Optional

import numpy as np
import pandas as pd

from config import (
    MIN_BASE_DAYS, EARNINGS_BLACKOUT_DAYS,
    MAX_FINAL_CONTRACTION_PCT, MIN_CONTRACTIONS, MAX_CONTRACTIONS,
    SWING_MACRO_N, SWING_CONTRACTION_N, SWING_MICRO_N,
    LSH_MIN_PULLBACK_PCT, PRIOR_UPTREND_MIN_PCT, MAX_STOP_PCT,
)
from news import has_earnings_blackout

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════
# GENERIC PIVOT HELPERS
# ═══════════════════════════════════════════════════════════


def find_swing_highs(high: pd.Series, n: int) -> list[int]:
    """Indices where high[i] > all of high[i-n:i] and high[i+1:i+n+1]."""
    vals = high.values
    result = []
    for i in range(n, len(vals) - n):
        if vals[i] > max(vals[i - n:i]) and vals[i] > max(vals[i + 1:i + n + 1]):
            result.append(i)
    return result


def find_swing_lows(low: pd.Series, n: int) -> list[int]:
    """Indices where low[i] < all of low[i-n:i] and low[i+1:i+n+1]."""
    vals = low.values
    result = []
    for i in range(n, len(vals) - n):
        if vals[i] < min(vals[i - n:i]) and vals[i] < min(vals[i + 1:i + n + 1]):
            result.append(i)
    return result


# ═══════════════════════════════════════════════════════════
# PASS 1 — MACRO: Left Side High + base boundary
# ═══════════════════════════════════════════════════════════


def _find_left_side_high(
    df: pd.DataFrame,
    n: int = SWING_MACRO_N,
) -> tuple[Optional[int], str]:
    """Identify the most recent macro swing high (LSH) that:
      1. Was followed by a pullback of >= LSH_MIN_PULLBACK_PCT
      2. Was preceded by an advance of >= PRIOR_UPTREND_MIN_PCT

    Returns (bar_index_in_df, "") on success, (None, reason) on failure.
    Searches backwards from today so the most recent qualifying LSH wins.
    """
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]

    macro_highs = find_swing_highs(high, n)
    if not macro_highs:
        return None, "no macro swing highs found (n={})".format(n)

    # Walk backwards through macro highs — most recent first
    for sh_idx in reversed(macro_highs):
        sh_price = float(high.iloc[sh_idx])

        # ── Check prior uptrend into this high ──
        # Look up to 252 bars before the high for a swing low to measure from
        lookback_start = max(0, sh_idx - 252)
        prior_lows_idx = find_swing_lows(low.iloc[lookback_start:sh_idx + 1], n)
        if not prior_lows_idx:
            continue
        # Prior swing low price (global low in that window)
        prior_low_price = float(low.iloc[lookback_start:sh_idx].min())
        if prior_low_price <= 0:
            continue
        advance_pct = (sh_price - prior_low_price) / prior_low_price
        if advance_pct < PRIOR_UPTREND_MIN_PCT:
            continue  # insufficient prior uptrend into this high

        # ── Check pullback after the high ──
        # Find the lowest price between sh_idx and end of data
        post_low = float(low.iloc[sh_idx + 1:].min()) if sh_idx + 1 < len(low) else sh_price
        pullback_pct = (sh_price - post_low) / sh_price
        if pullback_pct < LSH_MIN_PULLBACK_PCT:
            continue  # not enough pullback after this high — not an LSH

        return sh_idx, ""

    return None, (
        f"no macro swing high qualified as LSH "
        f"(need >={LSH_MIN_PULLBACK_PCT:.0%} pullback after, "
        f">={PRIOR_UPTREND_MIN_PCT:.0%} advance before)"
    )


# ═══════════════════════════════════════════════════════════
# PASS 2 — CONTRACTION: intermediate swings within base
# ═══════════════════════════════════════════════════════════


def _build_contractions_in_base(
    base_df: pd.DataFrame,
    n: int = SWING_CONTRACTION_N,
) -> list[dict]:
    """Find swing high→low contraction pairs within the base window.

    Returns list of dicts sorted chronologically:
      sh_idx, sl_idx (indices within base_df), sh_price, sl_price, depth (fraction)
    """
    high = base_df["High"]
    low  = base_df["Low"]

    sh_indices = find_swing_highs(high, n)
    sl_indices = find_swing_lows(low, n)

    if not sh_indices or not sl_indices:
        return []

    contractions = []
    used_lows: set[int] = set()

    for sh_idx in sh_indices:
        sh_price = float(high.iloc[sh_idx])
        # Pair with the first swing low that comes strictly after this swing high
        subsequent = [i for i in sl_indices if i > sh_idx and i not in used_lows]
        if not subsequent:
            continue
        sl_idx   = subsequent[0]
        sl_price = float(low.iloc[sl_idx])
        depth    = (sh_price - sl_price) / sh_price

        contractions.append({
            "sh_idx":   sh_idx,
            "sl_idx":   sl_idx,
            "sh_price": sh_price,
            "sl_price": sl_price,
            "depth":    depth,
        })
        used_lows.add(sl_idx)

    contractions.sort(key=lambda x: x["sh_idx"])
    return contractions


# ═══════════════════════════════════════════════════════════
# PASS 3 — MICRO: exact pivot + stop within final contraction
# ═══════════════════════════════════════════════════════════


def _micro_pivot_stop(
    base_df: pd.DataFrame,
    final_contraction: dict,
    n: int = SWING_MICRO_N,
) -> tuple[float, float]:
    """Within the final contraction window, use n=SWING_MICRO_N to find:
      pivot = highest high (used for order placement)
      stop  = lowest low  (stop loss)

    Falls back to the contraction's sh_price/sl_price if no micro pivots found.
    """
    sh_idx = final_contraction["sh_idx"]
    # Window: from final contraction's swing high to end of base_df
    window = base_df.iloc[sh_idx:]

    pivot = float(window["High"].max())
    stop  = float(window["Low"].min())
    return pivot, stop


# ═══════════════════════════════════════════════════════════
# MAIN DETECTOR
# ═══════════════════════════════════════════════════════════


def detect_vcp(ticker: str, df: pd.DataFrame) -> tuple[Optional[dict], str]:
    """3-tier hierarchical VCP detection on daily OHLCV data.

    Args:
        ticker: Stock symbol.
        df: Daily OHLCV DataFrame. DatetimeIndex. Minimum 252 bars.

    Returns (setup_dict, "") on success, (None, reason_str) on rejection.

    Setup dict keys:
        ticker, pivot_price, stop_loss_price, base_duration_days,
        base_duration_weeks, contraction_depths, final_contraction_depth,
        volume_dry_up, lsh_price, lsh_date, n_contractions, stop_pct_from_pivot
    """
    if len(df) < 252:
        return None, "insufficient history (<252 bars)"

    # ── Earnings blackout ──
    if has_earnings_blackout(ticker):
        return None, f"earnings within {EARNINGS_BLACKOUT_DAYS} days"

    # ── Pass 1: Find Left Side High ──
    lsh_idx, lsh_err = _find_left_side_high(df, n=SWING_MACRO_N)
    if lsh_idx is None:
        return None, f"LSH not found: {lsh_err}"

    # Base duration check
    base_duration_days = len(df) - 1 - lsh_idx
    if base_duration_days < MIN_BASE_DAYS:
        return None, (
            f"base too short ({base_duration_days}d < {MIN_BASE_DAYS}d required — "
            f"LSH was {base_duration_days} trading days ago)"
        )

    base_df = df.iloc[lsh_idx:]

    # ── Pass 2: Find contractions within base ──
    contractions = _build_contractions_in_base(base_df, n=SWING_CONTRACTION_N)

    if len(contractions) < MIN_CONTRACTIONS:
        return None, (
            f"too few contractions ({len(contractions)} found in base, "
            f"need {MIN_CONTRACTIONS})"
        )

    # Take the last MAX_CONTRACTIONS contractions
    recent = contractions[-MAX_CONTRACTIONS:]
    if len(recent) < MIN_CONTRACTIONS:
        return None, f"too few recent contractions ({len(recent)})"

    # Verify tightening
    depths = [c["depth"] for c in recent]
    for i in range(1, len(depths)):
        if depths[i] >= depths[i - 1]:
            return None, (
                f"not tightening: contraction {i} depth "
                f"{depths[i]:.1%} >= prior {depths[i-1]:.1%}"
            )

    # Final contraction depth check
    final_depth = depths[-1]
    if final_depth >= MAX_FINAL_CONTRACTION_PCT:
        return None, (
            f"final contraction {final_depth:.1%} >= "
            f"{MAX_FINAL_CONTRACTION_PCT:.0%} max"
        )

    # ── Pass 3: Micro pivot + stop in final contraction window ──
    pivot_price, stop_loss_price = _micro_pivot_stop(
        base_df, recent[-1], n=SWING_MICRO_N
    )

    stop_pct = (pivot_price - stop_loss_price) / pivot_price
    if stop_pct > MAX_STOP_PCT:
        return None, (
            f"stop {stop_pct:.1%} below pivot (max {MAX_STOP_PCT:.0%})"
        )

    # ── Volume dry-up ──
    final_sh_idx = recent[-1]["sh_idx"]
    final_sl_idx = recent[-1]["sl_idx"]
    final_vol    = base_df["Volume"].iloc[final_sh_idx:final_sl_idx + 1]
    avg_vol_50d  = (
        float(df["Volume"].iloc[-50:].mean())
        if len(df) >= 50 else float(df["Volume"].mean())
    )
    final_avg_vol = float(final_vol.mean()) if len(final_vol) > 0 else avg_vol_50d
    volume_dry_up = final_avg_vol < avg_vol_50d

    if not volume_dry_up:
        return None, (
            f"no volume dry-up in final contraction "
            f"(avg {final_avg_vol:,.0f} vs 50d avg {avg_vol_50d:,.0f})"
        )

    # ── LSH metadata ──
    lsh_price = float(df["High"].iloc[lsh_idx])
    lsh_date  = str(df.index[lsh_idx].date()) if hasattr(df.index, "date") else ""

    base_weeks = base_duration_days // 5  # trading days → approximate weeks

    logger.info(
        "VCP ✓ %s | pivot=%.2f stop=%.2f | base=%dd(%dw) | "
        "contractions=%s | LSH=%.2f",
        ticker, pivot_price, stop_loss_price,
        base_duration_days, base_weeks,
        [f"{d:.1%}" for d in depths], lsh_price,
    )

    return {
        "ticker":                 ticker,
        "pivot_price":            round(pivot_price, 2),
        "stop_loss_price":        round(stop_loss_price, 2),
        "base_duration_days":     base_duration_days,
        "base_duration_weeks":    base_weeks,
        "contraction_depths":     [round(d, 4) for d in depths],
        "final_contraction_depth": round(final_depth, 4),
        "n_contractions":         len(recent),
        "volume_dry_up":          volume_dry_up,
        "lsh_price":              round(lsh_price, 2),
        "lsh_date":               lsh_date,
        "stop_pct_from_pivot":    round(stop_pct, 4),
    }, ""


# ═══════════════════════════════════════════════════════════
# BATCH RUNNER
# ═══════════════════════════════════════════════════════════


def detect_vcp_batch(
    scan_results: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Run VCP detection on all scanner results.

    Args:
        scan_results: from scanner.scan_universe().
                      Each dict must have 'ticker', 'df', 'rs_rank'.

    Returns (vcp_setups, rejections):
      vcp_setups: merged scanner + VCP dicts, sorted by rs_rank desc.
      rejections: {ticker, phase, reason} for each failure.
    """
    vcp_setups:  list[dict] = []
    rejections:  list[dict] = []
    # Track single-contraction failures for HTF hand-off
    single_contraction_tickers: list[str] = []

    for stock in scan_results:
        ticker = stock["ticker"]
        df     = stock.get("df")
        if df is None:
            continue

        try:
            vcp, reason = detect_vcp(ticker, df)
        except Exception as e:
            logger.warning("VCP detection error %s: %s", ticker, e)
            rejections.append({"ticker": ticker, "phase": "VCP",
                                "reason": f"exception: {e}"})
            continue

        if vcp is None:
            rejections.append({"ticker": ticker, "phase": "VCP", "reason": reason})
            # Tag for HTF hand-off if the only failure was "too few contractions (1)"
            if "too few contractions (1" in reason:
                single_contraction_tickers.append(ticker)
            continue

        vcp_setups.append({
            **vcp,
            "rs_rank":  stock["rs_rank"],
            "rs_raw":   stock["rs_raw"],
            "close":    stock["close"],
            "sma50":    stock["sma50"],
            "sma150":   stock["sma150"],
            "sma200":   stock["sma200"],
            "adv50":    stock["adv50"],
        })

    vcp_setups.sort(key=lambda x: x["rs_rank"], reverse=True)
    logger.info(
        "VCP detector: %d setups from %d candidates "
        "(%d single-contraction → HTF candidate)",
        len(vcp_setups), len(scan_results), len(single_contraction_tickers),
    )

    # Attach single-contraction list to first rejection for HTF pipeline access
    # (main.py reads this via the returned rejections list)
    if single_contraction_tickers:
        rejections.append({
            "ticker": "__HTF_CANDIDATES__",
            "phase":  "VCP",
            "reason": ",".join(single_contraction_tickers),
        })

    return vcp_setups, rejections
