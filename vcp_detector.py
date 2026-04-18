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
    MAX_FINAL_CONTRACTION_STANDARD, MAX_FINAL_CONTRACTION_HIGH_BETA,
    VOLUME_DRY_STANDARD_PCT, VOLUME_DRY_STRICT_PCT,
    MIN_CONTRACTIONS, MAX_CONTRACTIONS,
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
# ATR HELPER
# ═══════════════════════════════════════════════════════════


def _compute_atr_pct(df: pd.DataFrame, period: int = 14) -> float:
    """ATR-14 as percentage of current close price."""
    if len(df) < period + 1:
        return 0.0
    close = df["Close"].values
    high  = df["High"].values
    low   = df["Low"].values
    tr_vals = []
    for i in range(len(df) - period, len(df)):
        hl = high[i] - low[i]
        hc = abs(high[i] - close[i - 1])
        lc = abs(low[i]  - close[i - 1])
        tr_vals.append(max(hl, hc, lc))
    atr = sum(tr_vals) / len(tr_vals)
    close_now = float(close[-1])
    return (atr / close_now * 100) if close_now > 0 else 0.0


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
    last_rejection = ""
    for sh_idx in reversed(macro_highs):
        sh_price = float(high.iloc[sh_idx])

        # ── Check prior uptrend into this high (CHANGE 3) ──
        lookback_start = max(0, sh_idx - 252)
        prior_lows_idx = find_swing_lows(low.iloc[lookback_start:sh_idx + 1], n)
        if not prior_lows_idx:
            last_rejection = "no prior swing low found within 252 bars | NO_PRIOR_UPTREND"
            continue
        prior_low_price = float(low.iloc[lookback_start:sh_idx].min())
        if prior_low_price <= 0:
            last_rejection = "invalid prior low price | NO_PRIOR_UPTREND"
            continue
        advance_pct = (sh_price - prior_low_price) / prior_low_price
        if advance_pct < PRIOR_UPTREND_MIN_PCT:
            last_rejection = (
                f"advance into high {advance_pct:.0%} < {PRIOR_UPTREND_MIN_PCT:.0%} required "
                f"| NO_PRIOR_UPTREND"
            )
            continue

        # ── Check pullback after the high ──
        post_low = float(low.iloc[sh_idx + 1:].min()) if sh_idx + 1 < len(low) else sh_price
        pullback_pct = (sh_price - post_low) / sh_price
        if pullback_pct < LSH_MIN_PULLBACK_PCT:
            last_rejection = (
                f"pullback after high {pullback_pct:.0%} < {LSH_MIN_PULLBACK_PCT:.0%} required "
                f"| INSUFFICIENT_PULLBACK"
            )
            continue

        return sh_idx, ""

    reason = last_rejection or "no macro swing highs found"
    return None, f"LSH not found | {len(macro_highs)} highs checked | {reason} | NO_LSH"


# ═══════════════════════════════════════════════════════════
# PASS 2 — CONTRACTION: intermediate swings within base
# ═══════════════════════════════════════════════════════════


def _build_contractions_in_base(
    base_df: pd.DataFrame,
    n: int = SWING_CONTRACTION_N,
    min_depth: float = 0.03,   # ignore swings shallower than 3%
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
        if sl_price >= sh_price:
            continue   # no real pullback (stock in uptrend past this high)
        depth    = (sh_price - sl_price) / sh_price
        if depth < min_depth:
            continue   # ignore micro-oscillations — not a structural contraction

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
    """Find exact entry pivot and stop within the post-contraction consolidation.

    Window starts at the final swing LOW (sl_idx) — i.e. the tight range AFTER
    the last pullback, not from the swing high.  This prevents deep prior lows
    from inflating the stop distance when the stock has rallied back from the low.
    """
    sl_idx = final_contraction["sl_idx"]
    window = base_df.iloc[sl_idx:]          # post-contraction consolidation

    if len(window) == 0:
        return final_contraction["sh_price"], final_contraction["sl_price"]

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
            f"VCP_REJECT | base={base_duration_days}d < {MIN_BASE_DAYS}d required | "
            f"BASE_TOO_SHORT (LSH was {base_duration_days} trading days ago)"
        )

    base_df = df.iloc[lsh_idx:]

    # ── ATR (computed once, used for min_depth and ceiling checks) ──
    atr_pct   = _compute_atr_pct(df)
    if atr_pct > 5.0:
        min_depth = 0.05   # very high ATR: filter below 5%
    else:
        min_depth = 0.03   # standard + high ATR: filter below 3%

    # ── Pass 2: Adaptive contraction detection (CHANGE 1) ──
    # Shorter bases can't support n=8 swings — scale n with base length
    if base_duration_days >= 60:
        contraction_n = SWING_CONTRACTION_N   # 8 — long bases, full sensitivity
    elif base_duration_days >= 35:
        contraction_n = 5                      # medium bases
    else:
        contraction_n = 3                      # short bases (4–7 weeks)

    contractions = _build_contractions_in_base(base_df, n=contraction_n, min_depth=min_depth)

    # ── Contraction count gate ──
    is_vcp_1c      = False
    pivot_price    = None
    stop_loss_price = None
    micro_depth_1c = None

    if len(contractions) < MIN_CONTRACTIONS:
        # VCP_1C path: accept single contraction when base is short
        # and the post-contraction range is ≥40% tighter (≤60% of single depth)
        if len(contractions) == 1 and base_duration_days < 35:
            single   = contractions[0]
            sl_start = single["sl_idx"]       # index within base_df
            post_sl  = base_df.iloc[sl_start:]
            if len(post_sl) < 3:
                return None, (
                    f"VCP_REJECT | base={base_duration_days}d | "
                    f"contractions=[{single['depth']:.1%}] | "
                    f"post-contraction only {len(post_sl)} bars | VCP_1C_MICRO_FAIL"
                )
            tight_pivot = float(post_sl["High"].max())
            tight_stop  = float(post_sl["Low"].min())
            micro_depth_1c = (tight_pivot - tight_stop) / tight_pivot if tight_pivot > 0 else 1.0
            required = single["depth"] * 0.60  # must be ≥40% shallower
            if micro_depth_1c > required:
                return None, (
                    f"VCP_REJECT | base={base_duration_days}d | "
                    f"contractions=[{single['depth']:.1%}] | "
                    f"micro={micro_depth_1c:.1%} > 60%×{single['depth']:.1%}={required:.1%} | "
                    f"VCP_1C_MICRO_FAIL"
                )
            is_vcp_1c       = True
            recent          = [single]
            pivot_price     = round(tight_pivot, 2)
            stop_loss_price = round(tight_stop, 2)
        else:
            return None, (
                f"VCP_REJECT | base={base_duration_days}d | "
                f"too few contractions ({len(contractions)} found in base, "
                f"need {MIN_CONTRACTIONS}) | NOT_ENOUGH_CONTRACTIONS"
            )
    else:
        # Standard VCP_2C+ path
        recent = contractions[-MAX_CONTRACTIONS:]
        if len(recent) < MIN_CONTRACTIONS:
            return None, (
                f"VCP_REJECT | base={base_duration_days}d | "
                f"too few recent contractions ({len(recent)}) | NOT_ENOUGH_CONTRACTIONS"
            )

        depths = [c["depth"] for c in recent]

        # Rule 1: Final contraction must be the tightest (absolute requirement)
        if depths[-1] != min(depths):
            return None, (
                f"VCP_REJECT | base={base_duration_days}d | "
                f"contractions={[f'{d:.1%}' for d in depths]} | "
                f"final={depths[-1]:.1%} is not the tightest | FINAL_NOT_TIGHTEST"
            )

        # Rule 2: Overall trend must be compressing (regression slope negative)
        if len(depths) >= 3:
            x     = np.arange(len(depths), dtype=float)
            slope = np.polyfit(x, depths, 1)[0]
            if slope >= 0:
                return None, (
                    f"VCP_REJECT | base={base_duration_days}d | "
                    f"contractions={[f'{d:.1%}' for d in depths]} | "
                    f"regression slope={slope:.4f} >= 0 (not compressing) | NOT_COMPRESSING"
                )

        # Rule 3: Final must be meaningfully tighter than the first
        # (prevents final=9.8% qualifying when first=10% — essentially flat base)
        if len(depths) >= 2:
            compression_ratio = depths[-1] / depths[0]
            if compression_ratio > 0.85:
                return None, (
                    f"VCP_REJECT | base={base_duration_days}d | "
                    f"contractions={[f'{d:.1%}' for d in depths]} | "
                    f"compression_ratio={compression_ratio:.2f} > 0.85 (insufficient compression) | "
                    f"INSUFFICIENT_COMPRESSION"
                )

    # ── Pass 3: Micro pivot + stop for VCP_2C+ ──
    if not is_vcp_1c:
        pivot_price, stop_loss_price = _micro_pivot_stop(
            base_df, recent[-1], n=SWING_MICRO_N
        )

    # Final contraction depth for threshold checks
    depths_all  = [c["depth"] for c in recent]
    final_depth = micro_depth_1c if is_vcp_1c else recent[-1]["depth"]

    # ── ATR-adjusted absolute depth ceiling ──
    max_final = (
        MAX_FINAL_CONTRACTION_HIGH_BETA if atr_pct > 3.5
        else MAX_FINAL_CONTRACTION_STANDARD
    )
    if final_depth > max_final:
        return None, (
            f"VCP_REJECT | base={base_duration_days}d | "
            f"contractions={[f'{d:.1%}' for d in depths_all]} | "
            f"final={final_depth:.1%} > {max_final:.0%} ceiling (ATR={atr_pct:.1f}%) | "
            f"FINAL_CONTRACTION_WIDE"
        )

    # ── Stop width check ──
    stop_pct = (pivot_price - stop_loss_price) / pivot_price
    if stop_pct > MAX_STOP_PCT:
        return None, (
            f"VCP_REJECT | base={base_duration_days}d | "
            f"contractions={[f'{d:.1%}' for d in depths_all]} | "
            f"stop={stop_pct:.1%} > {MAX_STOP_PCT:.0%} max | STOP_TOO_WIDE"
        )

    # ── Volume dry-up with compensation matrix (CHANGE 2) ──
    if is_vcp_1c:
        vol_window = base_df["Volume"].iloc[recent[0]["sl_idx"]:]
    else:
        final_sh_idx = recent[-1]["sh_idx"]
        final_sl_idx = recent[-1]["sl_idx"]
        vol_window   = base_df["Volume"].iloc[final_sh_idx:final_sl_idx + 1]

    avg_vol_50d   = (
        float(df["Volume"].iloc[-50:].mean())
        if len(df) >= 50 else float(df["Volume"].mean())
    )
    final_avg_vol = float(vol_window.mean()) if len(vol_window) > 0 else avg_vol_50d
    vol_threshold = VOLUME_DRY_STANDARD_PCT if final_depth <= 0.08 else VOLUME_DRY_STRICT_PCT
    vol_ratio     = final_avg_vol / avg_vol_50d if avg_vol_50d > 0 else 1.0

    if vol_ratio >= vol_threshold:
        return None, (
            f"VCP_REJECT | base={base_duration_days}d | "
            f"contractions={[f'{d:.1%}' for d in depths_all]} | "
            f"final_depth={final_depth:.1%} | "
            f"vol_ratio={vol_ratio:.2f} >= {vol_threshold:.2f} threshold | "
            f"VOLUME_DRY_FAIL"
        )

    # ── LSH metadata ──
    lsh_price  = float(df["High"].iloc[lsh_idx])
    lsh_date   = str(df.index[lsh_idx].date()) if hasattr(df.index, "date") else ""
    base_weeks = base_duration_days // 5
    pat_label  = "VCP_1C" if is_vcp_1c else "VCP"

    logger.info(
        "%s ✓ %s | pivot=%.2f stop=%.2f | base=%dd(%dw) | "
        "contractions=%s | ATR=%.1f%% | LSH=%.2f",
        pat_label, ticker, pivot_price, stop_loss_price,
        base_duration_days, base_weeks,
        [f"{d:.1%}" for d in depths_all], atr_pct, lsh_price,
    )

    return {
        "ticker":                  ticker,
        "pivot_price":             round(pivot_price, 2),
        "stop_loss_price":         round(stop_loss_price, 2),
        "base_duration_days":      base_duration_days,
        "base_duration_weeks":     base_weeks,
        "contraction_depths":      [round(d, 4) for d in depths_all],
        "final_contraction_depth": round(final_depth, 4),
        "n_contractions":          len(recent),
        "volume_dry_up":           True,
        "lsh_price":               round(lsh_price, 2),
        "lsh_date":                lsh_date,
        "stop_pct_from_pivot":     round(stop_pct, 4),
        "pattern_type":            pat_label,
        "atr_pct":                 round(atr_pct, 2),
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
            # HTF hand-off: single-contraction failures on longer bases
            # (VCP_1C path already accepted short-base single contractions)
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
