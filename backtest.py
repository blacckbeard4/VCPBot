"""VCP Strategy Backtest — 10-year walk-forward simulation.

Simulates the exact same strategy logic the live bot uses:
  - Regime filter (SPY SMA200 → CASH / NORMAL)
  - Phase 2 hard gates (price > $10, ADV50 > 1M, close > SMA200)
  - Phase 3 trend template (all 7 checks)
  - Phase 4 VCP pattern detection (contractions, volume dry-up, pivot)
  - Phase 5 risk sizing (2% equity per trade, max 5 positions)
  - Entry: buy stop at pivot + $0.05 (triggered next trading day)
  - Exit: stop loss or +20% take-profit, max 25-day hold
  - Scans weekly (every Friday close) to keep runtime reasonable

Limitations / known biases:
  - Survivorship bias: only current tickers used (delisted losers excluded)
  - Earnings blackout NOT simulated (historical earnings dates unavailable)
  - RVOL intraday filter NOT simulated (daily bars only)
  - Entry fills assume trigger crossed if next day high >= pivot + $0.05
  - Same-day stop + target: stop assumed hit first (conservative)

Usage:
  python backtest.py
  python backtest.py --start 2015 --end 2024 --capital 25000
  python backtest.py --tickers AAPL MSFT NVDA TSLA --start 2020
"""

import argparse
import gc
import logging
import sys
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd
import yfinance as yf

# ── Reuse live bot's VCP detection (no earnings blackout in backtest) ──
import news as _news_mod
_news_mod.has_earnings_blackout = lambda ticker: False  # skip — can't backtest historically

from vcp_detector import detect_vcp
from htf_detector import detect_htf
from config import (
    MIN_AVG_VOLUME, MIN_PRICE, RS_RANK_CURRENT_MIN,
    MIN_PCT_ABOVE_52W_LOW, MAX_PCT_BELOW_52W_HIGH,
    BUY_STOP_OFFSET, TARGET_PCT, MAX_STOP_PCT,
    RISK_PCT_NORMAL, MAX_OPEN_POSITIONS,
)

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ── Default universe: S&P 100 cross-sector large caps ──────────────────────
DEFAULT_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO",
    "JPM", "LLY", "V", "UNH", "XOM", "MA", "JNJ", "PG", "HD", "COST",
    "ABBV", "MRK", "WMT", "CVX", "KO", "PEP", "BAC", "NFLX", "AMD", "TMO",
    "ORCL", "DIS", "ADBE", "CRM", "ABT", "CSCO", "MCD", "ACN", "DHR",
    "TXN", "PFE", "NKE", "NEE", "INTC", "RTX", "HON", "INTU",
    "IBM", "UPS", "GE", "CAT", "MS", "AMGN", "GS", "SPGI", "MDT", "BLK",
    "BA", "SYK", "BKNG", "AXP", "GILD", "C", "SBUX",
    "CB", "ZTS", "VRTX", "CVS", "DE", "CI", "TJX", "SO", "DUK",
    "REGN", "PGR", "ELV", "BSX", "EMR", "ITW", "WM", "AON", "CME",
    "FDX", "NOC", "GD", "LMT", "MMC", "USB", "F", "GM", "COF",
]

# ── Growth/momentum universe — the natural home of VCP setups ───────────────
# Includes semis, cloud/SaaS, biotech, consumer growth, fintech, and known
# momentum names from 2015-2024. Newer IPOs (SNOW, CRWD, etc.) contribute
# only from their listing date onward — no data, no trades, no bias.
GROWTH_TICKERS = [
    # Semiconductors & equipment
    "NVDA", "AMD", "AVGO", "MRVL", "AMAT", "LRCX", "KLAC", "SNPS", "CDNS",
    "MPWR", "ENTG", "ON", "SMCI", "ONTO", "WOLF", "AMBA", "SLAB",

    # Software / Cloud / SaaS
    "CRM", "NOW", "WDAY", "ADBE", "INTU", "VEEV", "HUBS", "TWLO", "DDOG",
    "NET", "ZS", "CRWD", "PANW", "FTNT", "OKTA", "MDB", "SNOW", "PAYC",
    "DOCU", "ZM", "BILL", "PCTY", "APPN", "SMAR", "RNG",

    # Internet / E-commerce
    "NFLX", "AMZN", "GOOGL", "META", "SHOP", "MELI", "SE", "ETSY",
    "BKNG", "ABNB", "UBER", "DASH", "PINS", "SNAP",

    # Fintech / Payments
    "SQ", "PYPL", "COIN", "MA", "V", "AFRM", "SOFI",

    # Biotech / MedTech
    "ISRG", "DXCM", "PODD", "INSP", "ALGN", "IRTC", "EXAS",
    "BNTX", "MRNA", "ILMN", "IDXX", "HOLX", "TMDX",

    # Consumer growth & brands
    "LULU", "FIVE", "MNST", "CELH", "CMG", "ULTA", "RH", "DECK",
    "ONON", "SKX", "WING", "ELF", "BROS", "BURL",

    # Energy transition
    "ENPH", "FSLR", "SEDG", "RUN",

    # Industrials / construction growth
    "BLDR", "IBP", "TREX", "BECN", "AXON", "FERG",

    # Financial data / exchanges
    "MKTX", "MSCI", "FDS", "CBOE", "ICE", "SPGI",

    # Misc high-momentum names
    "TSLA", "AAPL", "MSFT",  # mega-caps that have had VCP years
    "TTD", "ROKU", "U", "RBLX", "DUOL", "GTLB",
    "CPNG", "JD", "PDD",     # international growth
    "AEHR", "AAON", "EXPO",  # hidden gems with strong VCP history
]


# ═══════════════════════════════════════════════════════════
# DATA DOWNLOAD
# ═══════════════════════════════════════════════════════════


_CACHE_FILE = "backtest_data_cache.pkl"


def download_data(tickers: list[str], start_year: int, end_year: int) -> dict[str, pd.DataFrame]:
    """Download 10+ years of daily OHLCV for all tickers + SPY.

    Caches results to backtest_data_cache.pkl so re-runs don't re-download.
    Uses per-ticker Ticker.history() to avoid batch rate limits.

    Returns dict: ticker → DataFrame. Tickers with < 400 bars are dropped.
    """
    import pickle
    from pathlib import Path

    all_tickers = list(set(["SPY"] + tickers))
    dl_start = f"{start_year - 1}-01-01"
    dl_end   = f"{end_year}-12-31"
    cache_path = Path(_CACHE_FILE)

    # Load existing cache
    cached: dict[str, pd.DataFrame] = {}
    if cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            print(f"Loaded cache: {len(cached)} tickers already downloaded.")
        except Exception:
            cached = {}

    needed = [t for t in all_tickers if t not in cached]
    if not needed:
        print(f"All {len(all_tickers)} tickers loaded from cache.\n")
        return {t: cached[t] for t in all_tickers if t in cached}

    print(f"Downloading {len(needed)} tickers ({dl_start} → {dl_end})...")
    print(f"  {len(cached)} already cached. Downloading one-by-one with 3s gaps.")
    print(f"  Progress saved after each ticker — safe to Ctrl+C and resume.\n")

    for idx, ticker in enumerate(needed):
        print(f"  [{idx+1}/{len(needed)}] {ticker}...", end=" ", flush=True)

        df = None
        for attempt in range(5):
            try:
                df = yf.Ticker(ticker).history(
                    start=dl_start, end=dl_end, interval="1d", auto_adjust=True,
                )
                if df is not None and len(df) >= 400:
                    break
                df = None
                time.sleep(10)
            except Exception as e:
                wait = 20 * (attempt + 1)
                print(f"\n    attempt {attempt+1}/5 failed — waiting {wait}s...", end=" ", flush=True)
                time.sleep(wait)

        if df is not None and len(df) >= 400:
            # yf.Ticker.history() returns Open/High/Low/Close/Volume columns
            df.index = pd.to_datetime(df.index).tz_localize(None)
            cached[ticker] = df
            print(f"ok ({len(df)} bars)")
        else:
            print("skipped (insufficient data)")

        # Save cache after every ticker so progress survives interruptions
        try:
            with open(cache_path, "wb") as f:
                pickle.dump(cached, f)
        except Exception:
            pass

        if idx < len(needed) - 1:
            time.sleep(3)

    result = {t: cached[t] for t in all_tickers if t in cached}
    print(f"\nDownload complete: {len(result)} tickers with sufficient history.\n")
    return result


# ═══════════════════════════════════════════════════════════
# INDICATOR HELPERS
# ═══════════════════════════════════════════════════════════


def _sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n, min_periods=n).mean()


def _spy_is_cash_mode(spy_slice: pd.DataFrame) -> bool:
    """True if SPY is in CASH MODE (close < SMA200)."""
    if len(spy_slice) < 200:
        return True  # not enough history — stay out
    close = spy_slice["Close"]
    sma200 = _sma(close, 200).iloc[-1]
    if pd.isna(sma200):
        return True
    return float(close.iloc[-1]) < float(sma200)


def _passes_phase2(df_slice: pd.DataFrame) -> bool:
    """Phase 2: price > $10, ADV50 > 1M, close > SMA200."""
    if len(df_slice) < 200:
        return False
    close = df_slice["Close"]
    vol   = df_slice["Volume"]

    c = float(close.iloc[-1])
    if c < MIN_PRICE:
        return False

    adv50 = float(vol.iloc[-50:].mean()) if len(vol) >= 50 else float(vol.mean())
    if adv50 < MIN_AVG_VOLUME:
        return False

    sma200 = _sma(close, 200).iloc[-1]
    if pd.isna(sma200) or c < float(sma200):
        return False

    return True


def _passes_trend_template(df_slice: pd.DataFrame) -> bool:
    """Phase 3: all 7 Minervini trend template checks."""
    if len(df_slice) < 252:
        return False

    close = df_slice["Close"]
    high  = df_slice["High"]
    low   = df_slice["Low"]

    sma50  = _sma(close, 50)
    sma150 = _sma(close, 150)
    sma200 = _sma(close, 200)

    c    = float(close.iloc[-1])
    s50  = float(sma50.iloc[-1])
    s150 = float(sma150.iloc[-1])
    s200 = float(sma200.iloc[-1])

    if any(pd.isna(x) for x in [s50, s150, s200]):
        return False

    s200_30d = float(sma200.iloc[-31]) if len(sma200) > 31 and not pd.isna(sma200.iloc[-31]) else None

    if not (c > s150 and c > s200):        return False
    if s150 <= s200:                        return False
    if s200_30d is None or s200 <= s200_30d: return False
    if not (s50 > s150 and s50 > s200):    return False
    if c <= s50:                            return False

    w52_low  = float(low.iloc[-252:].min())
    w52_high = float(high.iloc[-252:].max())

    if c < w52_low  * (1 + MIN_PCT_ABOVE_52W_LOW):  return False
    if c < w52_high * (1 - MAX_PCT_BELOW_52W_HIGH): return False

    return True


def _compute_rs_raw(close: pd.Series) -> float | None:
    """Minervini RS composite: 0.4*ROC63 + 0.2*(ROC126+ROC189+ROC252)."""
    if len(close) < 253:
        return None
    def roc(n):
        return (float(close.iloc[-1]) - float(close.iloc[-n-1])) / float(close.iloc[-n-1]) * 100
    return 0.4*roc(63) + 0.2*roc(126) + 0.2*roc(189) + 0.2*roc(252)


# ═══════════════════════════════════════════════════════════
# ENTRY / EXIT SIMULATION
# ═══════════════════════════════════════════════════════════


def _simulate_entry(df: pd.DataFrame, signal_idx: int, trigger: float) -> tuple[int, float] | None:
    """Try to fill a buy stop at `trigger` on the next trading day after signal_idx.

    Returns (fill_bar_idx, fill_price) or None if never triggered.
    Only tries the single next bar (GTC buy stop expires after 1 day like the live bot).
    """
    next_idx = signal_idx + 1
    if next_idx >= len(df):
        return None

    row = df.iloc[next_idx]
    day_high = float(row["High"])
    day_open = float(row["Open"])

    # Gap up open above trigger → fill at open
    if day_open >= trigger:
        return next_idx, round(day_open, 2)

    # Intraday crosses trigger
    if day_high >= trigger:
        return next_idx, round(trigger, 2)

    return None  # never triggered → order expires


def _simulate_exit(
    df: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    stop_price: float,
    target_price: float,
    max_hold_bars: int = 25,
) -> tuple[int, float, str]:
    """Walk forward from entry_idx looking for stop, target, or max-hold exit.

    Returns (exit_bar_idx, exit_price, reason).
    Same-day stop + target: stop assumed hit first (conservative).
    """
    for i in range(entry_idx + 1, min(entry_idx + max_hold_bars + 1, len(df))):
        row = df.iloc[i]
        day_low  = float(row["Low"])
        day_high = float(row["High"])
        day_open = float(row["Open"])

        # Gap down through stop → fill at open
        if day_open <= stop_price:
            return i, round(day_open, 2), "STOPPED"

        # Gap up through target → fill at open
        if day_open >= target_price:
            return i, round(day_open, 2), "TARGET_HIT"

        # Intraday stop before target (conservative — stop first)
        if day_low <= stop_price:
            return i, round(stop_price, 2), "STOPPED"

        if day_high >= target_price:
            return i, round(target_price, 2), "TARGET_HIT"

    # Max hold expired
    last_idx = min(entry_idx + max_hold_bars, len(df) - 1)
    return last_idx, round(float(df.iloc[last_idx]["Close"]), 2), "EXPIRED"


# ═══════════════════════════════════════════════════════════
# CORE BACKTEST LOOP
# ═══════════════════════════════════════════════════════════


def run_backtest(
    tickers: list[str],
    data: dict[str, pd.DataFrame],
    start_year: int,
    end_year: int,
    initial_capital: float,
) -> tuple[list[dict], pd.Series]:
    """Walk-forward backtest. Scans every Friday. Returns (trades, equity_curve)."""

    spy_df = data.get("SPY")
    if spy_df is None:
        print("ERROR: SPY data missing — cannot run regime filter.")
        sys.exit(1)

    # Build trading calendar from SPY index
    all_dates: pd.DatetimeIndex = spy_df.index
    backtest_dates = all_dates[
        (all_dates >= f"{start_year}-01-01") &
        (all_dates <= f"{end_year}-12-31")
    ]

    # Scan on Fridays only (or last day of week if Friday is a holiday)
    scan_dates = []
    for dt in backtest_dates:
        if dt.weekday() == 4:  # Friday
            scan_dates.append(dt)
        elif dt.weekday() < 4:
            # If next day is past end of backtest, include as end-of-week
            pass
    # Ensure we always have at least end-of-week coverage
    seen_weeks: set[tuple] = set()
    scan_dates_set = set(scan_dates)
    for dt in reversed(backtest_dates):
        week_key = (dt.isocalendar()[0], dt.isocalendar()[1])
        if week_key not in seen_weeks:
            seen_weeks.add(week_key)
            if dt not in scan_dates_set:
                scan_dates.append(dt)
    scan_dates = sorted(set(scan_dates))

    equity         = initial_capital
    equity_curve   = {}
    all_trades     = []
    open_positions: list[dict] = []   # active positions
    cooldowns:      dict[str, pd.Timestamp] = {}  # ticker → cooldown_until

    total_weeks = len(scan_dates)
    print(f"Running backtest: {start_year}–{end_year} | {len(tickers)} tickers | "
          f"{total_weeks} scan weeks | Starting equity: ${initial_capital:,.0f}\n")

    for week_num, scan_dt in enumerate(scan_dates):
        if week_num % 52 == 0:
            yr = scan_dt.year
            print(f"  Scanning {yr}... (equity ${equity:,.0f})", flush=True)

        # ── Get bar index in SPY for this scan date ──
        spy_loc = max(0, spy_df.index.searchsorted(scan_dt, side="right") - 1)

        # ── Step 1: Check open positions for exits that happened this week ──
        still_open = []
        for pos in open_positions:
            ticker = pos["ticker"]
            if ticker not in data:
                continue
            tk_df = data[ticker]

            # Find bar indices from last check to today
            last_check_idx = pos.get("last_check_idx", pos["entry_idx"])
            try:
                today_idx = max(0, tk_df.index.searchsorted(scan_dt, side="right") - 1)
            except Exception:
                still_open.append(pos)
                continue

            # Walk from last_check_idx+1 to today_idx to find any exits
            exit_found = False
            for bar_i in range(last_check_idx + 1, today_idx + 1):
                if bar_i >= len(tk_df):
                    break
                row = tk_df.iloc[bar_i]
                day_low  = float(row["Low"])
                day_high = float(row["High"])
                day_open = float(row["Open"])
                bar_dt   = tk_df.index[bar_i]

                stop   = pos["stop_price"]
                target = pos["target_price"]

                exit_price  = None
                exit_reason = None

                if day_open <= stop:
                    exit_price, exit_reason = day_open, "STOPPED"
                elif day_open >= target:
                    exit_price, exit_reason = day_open, "TARGET_HIT"
                elif day_low <= stop:
                    exit_price, exit_reason = stop, "STOPPED"
                elif day_high >= target:
                    exit_price, exit_reason = target, "TARGET_HIT"
                elif bar_i >= pos["entry_idx"] + 25:
                    exit_price, exit_reason = float(row["Close"]), "EXPIRED"

                if exit_price is not None:
                    pnl   = (exit_price - pos["entry_price"]) * pos["shares"]
                    pnl_r = pnl / (pos["risk_amount"]) if pos["risk_amount"] != 0 else 0

                    trade = {**pos, "exit_date": str(bar_dt.date()),
                             "exit_price": round(exit_price, 2),
                             "exit_reason": exit_reason,
                             "pnl": round(pnl, 2),
                             "r_multiple": round(pnl_r, 2)}
                    all_trades.append(trade)
                    equity += pnl
                    cooldowns[ticker] = bar_dt + timedelta(days=25)
                    exit_found = True
                    break

            if not exit_found:
                pos["last_check_idx"] = today_idx
                still_open.append(pos)

        open_positions = still_open

        # ── Step 2: Regime filter ──
        spy_slice = spy_df.iloc[:spy_loc + 1]
        if _spy_is_cash_mode(spy_slice):
            equity_curve[scan_dt] = equity
            continue

        # ── Step 3: Compute RS raw for all tickers (for ranking) ──
        rs_scores: dict[str, float] = {}
        for ticker in tickers:
            if ticker not in data:
                continue
            tk_df = data[ticker]
            try:
                tk_loc = max(0, tk_df.index.searchsorted(scan_dt, side="right") - 1)
            except Exception:
                continue
            tk_slice = tk_df.iloc[:tk_loc + 1]
            rs = _compute_rs_raw(tk_slice["Close"])
            if rs is not None:
                rs_scores[ticker] = rs

        # Percentile rank RS scores
        if not rs_scores:
            equity_curve[scan_dt] = equity
            continue

        sorted_rs = sorted(rs_scores.values())
        n_rs = len(sorted_rs)
        def rs_percentile(score: float) -> float:
            import bisect
            rank = bisect.bisect_left(sorted_rs, score)
            return round((rank / (n_rs - 1)) * 98 + 1, 1) if n_rs > 1 else 50.0

        # ── Step 4: Scan candidates ──
        slots_available = MAX_OPEN_POSITIONS - len(open_positions)
        if slots_available <= 0:
            equity_curve[scan_dt] = equity
            continue

        candidates = []
        already_open = {p["ticker"] for p in open_positions}

        for ticker in tickers:
            if ticker in already_open:
                continue
            if ticker in cooldowns and scan_dt < cooldowns[ticker]:
                continue
            if ticker not in data or ticker not in rs_scores:
                continue

            rs_rank = rs_percentile(rs_scores[ticker])
            if rs_rank < RS_RANK_CURRENT_MIN:
                continue

            tk_df = data[ticker]
            try:
                tk_loc = max(0, tk_df.index.searchsorted(scan_dt, side="right") - 1)
            except Exception:
                continue
            tk_slice = tk_df.iloc[:tk_loc + 1]

            if not _passes_phase2(tk_slice):
                continue
            if not _passes_trend_template(tk_slice):
                continue

            # VCP detection first, then HTF fallback
            setup = None
            pattern_type = None
            try:
                vcp, _ = detect_vcp(ticker, tk_slice)
                if vcp is not None:
                    setup = vcp
                    pattern_type = "VCP"
            except Exception:
                pass

            if setup is None:
                try:
                    htf, _ = detect_htf(ticker, tk_slice)
                    if htf is not None:
                        setup = htf
                        pattern_type = "HTF"
                except Exception:
                    pass

            if setup is None:
                continue

            candidates.append({
                "ticker":       ticker,
                "rs_rank":      rs_rank,
                "scan_idx":     tk_loc,
                "vcp":          setup,
                "pattern_type": pattern_type,
            })

        # Sort by RS rank (best first)
        candidates.sort(key=lambda x: x["rs_rank"], reverse=True)

        # ── Step 5: Simulate entries for top candidates ──
        for cand in candidates[:slots_available]:
            ticker       = cand["ticker"]
            vcp          = cand["vcp"]
            scan_idx     = cand["scan_idx"]
            pattern_type = cand["pattern_type"]
            tk_df        = data[ticker]

            pivot       = vcp["pivot_price"]
            stop        = vcp["stop_loss_price"]
            trigger     = round(pivot + BUY_STOP_OFFSET, 2)
            target      = round(trigger * (1 + TARGET_PCT), 2)
            risk_unit   = trigger - stop

            if risk_unit <= 0:
                continue

            fill = _simulate_entry(tk_df, scan_idx, trigger)
            if fill is None:
                cooldowns[ticker] = scan_dt + timedelta(days=7)
                continue

            entry_idx, entry_price = fill
            entry_dt = tk_df.index[entry_idx]

            risk_amount = equity * RISK_PCT_NORMAL
            shares      = round(risk_amount / risk_unit, 4)

            # Cost check — don't blow entire equity on one trade
            position_cost = shares * entry_price
            if position_cost > equity * 0.40:  # max 40% of equity in one position
                shares = round((equity * 0.40) / entry_price, 4)

            open_positions.append({
                "ticker":        ticker,
                "entry_date":    str(entry_dt.date()),
                "entry_price":   round(entry_price, 2),
                "stop_price":    round(stop, 2),
                "target_price":  round(target, 2),
                "shares":        shares,
                "risk_amount":   risk_amount,
                "rs_rank":       cand["rs_rank"],
                "base_weeks":    vcp["base_duration_weeks"],
                "contractions":  vcp["contraction_depths"],
                "pattern_type":  pattern_type,
                "entry_idx":     entry_idx,
                "last_check_idx": entry_idx,
                "year":          entry_dt.year,
            })

            cooldowns[ticker] = entry_dt + timedelta(days=30)
            slots_available -= 1
            if slots_available <= 0:
                break

        equity_curve[scan_dt] = equity

    # ── Close any still-open positions at last available price ──
    for pos in open_positions:
        ticker = pos["ticker"]
        if ticker not in data:
            continue
        tk_df = data[ticker]
        last_close = float(tk_df["Close"].iloc[-1])
        pnl = (last_close - pos["entry_price"]) * pos["shares"]
        all_trades.append({**pos,
            "exit_date":   str(tk_df.index[-1].date()),
            "exit_price":  round(last_close, 2),
            "exit_reason": "OPEN_AT_END",
            "pnl":         round(pnl, 2),
            "r_multiple":  round(pnl / pos["risk_amount"], 2) if pos["risk_amount"] else 0,
        })
        equity += pnl

    equity_curve[max(equity_curve.keys()) if equity_curve else scan_dates[-1]] = equity
    return all_trades, pd.Series(equity_curve)


# ═══════════════════════════════════════════════════════════
# REPORTING
# ═══════════════════════════════════════════════════════════


def _spy_annual_return(spy_df: pd.DataFrame, year: int) -> float:
    """SPY buy-and-hold return for a given year."""
    yr_data = spy_df[spy_df.index.year == year]["Close"]
    if len(yr_data) < 2:
        return 0.0
    return float((yr_data.iloc[-1] - yr_data.iloc[0]) / yr_data.iloc[0])


def print_report(
    trades: list[dict],
    equity_curve: pd.Series,
    spy_df: pd.DataFrame,
    initial_capital: float,
    start_year: int,
    end_year: int,
) -> None:
    """Print per-year performance table + overall stats."""

    if not trades:
        print("No trades found in backtest period.")
        return

    df = pd.DataFrame(trades)
    df["pnl"] = df["pnl"].astype(float)
    df["entry_year"] = pd.to_datetime(df["entry_date"]).dt.year

    # ── Per-year equity rebuild ──
    equity_ts = equity_curve.sort_index()
    year_start_equity: dict[int, float] = {}
    year_end_equity:   dict[int, float] = {}

    for yr in range(start_year, end_year + 1):
        yr_pts = equity_ts[equity_ts.index.year == yr]
        if not yr_pts.empty:
            year_start_equity[yr] = float(yr_pts.iloc[0])
            year_end_equity[yr]   = float(yr_pts.iloc[-1])
        else:
            year_start_equity[yr] = year_end_equity.get(yr - 1, initial_capital)
            year_end_equity[yr]   = year_start_equity[yr]

    # ── Header ──
    sep = "─" * 100
    print("\n" + "═" * 100)
    print(" VCP STRATEGY BACKTEST RESULTS")
    print("═" * 100)
    print(f"  Universe: {start_year}–{end_year} (survivorship bias applies)")
    print(f"  Period  : {start_year}–{end_year}")
    print(f"  Capital : ${initial_capital:,.0f} starting")
    print(f"  Rules   : 2% risk/trade | max 5 positions | +20% target | 7% stop | weekly scan")
    print("  NOTE    : Earnings blackout & RVOL filter NOT simulated")
    print("═" * 100)

    print(f"\n{'Year':<6} {'Trades':>7} {'Win%':>6} {'AvgWin':>8} {'AvgLoss':>8} "
          f"{'Net P&L':>10} {'Strat%':>8} {'SPY%':>7} {'Alpha':>7} {'End Eq':>12}")
    print(sep)

    total_pnl = 0.0
    for yr in range(start_year, end_year + 1):
        yr_trades = df[df["entry_year"] == yr]
        closed    = yr_trades[yr_trades["exit_reason"] != "OPEN_AT_END"]
        n         = len(closed)

        if n == 0:
            spy_ret = _spy_annual_return(spy_df, yr)
            eq_end  = year_end_equity.get(yr, initial_capital)
            print(f"{yr:<6} {'—':>7} {'—':>6} {'—':>8} {'—':>8} {'—':>10} "
                  f"{'—':>8} {spy_ret*100:>6.1f}% {'—':>7} ${eq_end:>11,.0f}")
            continue

        wins   = closed[closed["pnl"] > 0]["pnl"]
        losses = closed[closed["pnl"] <= 0]["pnl"]
        win_rt = len(wins) / n * 100
        avg_w  = wins.mean()  if len(wins)   > 0 else 0
        avg_l  = losses.mean() if len(losses) > 0 else 0
        net    = closed["pnl"].sum()
        total_pnl += net

        eq_start = year_start_equity.get(yr, initial_capital)
        eq_end   = year_end_equity.get(yr, eq_start)
        strat_rt = (eq_end - eq_start) / eq_start * 100 if eq_start > 0 else 0
        spy_ret  = _spy_annual_return(spy_df, yr) * 100
        alpha    = strat_rt - spy_ret

        print(f"{yr:<6} {n:>7} {win_rt:>5.0f}% {avg_w:>8.0f} {avg_l:>8.0f} "
              f"{net:>+10,.0f} {strat_rt:>+7.1f}% {spy_ret:>+6.1f}% {alpha:>+6.1f}% "
              f"${eq_end:>11,.0f}")

    print(sep)

    # ── Overall stats ──
    closed_all = df[df["exit_reason"] != "OPEN_AT_END"]
    n_all      = len(closed_all)
    wins_all   = closed_all[closed_all["pnl"] > 0]
    losses_all = closed_all[closed_all["pnl"] <= 0]

    final_equity = float(equity_curve.iloc[-1]) if len(equity_curve) > 0 else initial_capital
    total_return = (final_equity - initial_capital) / initial_capital * 100
    n_years      = end_year - start_year + 1
    cagr         = ((final_equity / initial_capital) ** (1.0 / n_years) - 1) * 100 if n_years > 0 else 0

    # Max drawdown from equity curve
    eq_vals = equity_curve.values
    peak    = np.maximum.accumulate(eq_vals)
    dd      = (eq_vals - peak) / peak
    max_dd  = float(dd.min()) * 100

    # SPY total return over same period
    spy_start = spy_df[spy_df.index.year == start_year]["Close"].iloc[0]
    spy_end   = spy_df[spy_df.index.year == end_year]["Close"].iloc[-1]
    spy_total = (spy_end - spy_start) / spy_start * 100
    spy_cagr  = ((spy_end / spy_start) ** (1.0 / n_years) - 1) * 100

    avg_win_all  = wins_all["pnl"].mean()   if len(wins_all)   > 0 else 0
    avg_loss_all = losses_all["pnl"].mean() if len(losses_all) > 0 else 0
    win_rate_all = len(wins_all) / n_all    if n_all > 0 else 0
    expectancy   = win_rate_all * avg_win_all + (1 - win_rate_all) * avg_loss_all

    avg_r = closed_all["r_multiple"].mean() if n_all > 0 else 0

    print(f"\n{'OVERALL SUMMARY':}")
    print(f"  Total trades      : {n_all}  ({len(wins_all)}W / {len(losses_all)}L)")
    print(f"  Win rate          : {win_rate_all*100:.1f}%")
    print(f"  Avg win / loss    : ${avg_win_all:+,.0f} / ${avg_loss_all:+,.0f}")
    print(f"  Avg R-multiple    : {avg_r:+.2f}R")
    print(f"  Expectancy/trade  : ${expectancy:+,.0f}")
    print(f"  Total P&L         : ${total_pnl:+,.0f}")
    print(f"  Final equity      : ${final_equity:,.0f}")
    print(f"  Total return      : {total_return:+.1f}%")
    print(f"  CAGR              : {cagr:+.1f}%")
    print(f"  Max drawdown      : {max_dd:.1f}%")
    print(f"\n  SPY buy-and-hold  : {spy_total:+.1f}% total | CAGR {spy_cagr:+.1f}%")
    print(f"  Alpha vs SPY      : {total_return - spy_total:+.1f}% total | CAGR {cagr - spy_cagr:+.1f}%")

    # ── Pattern type breakdown ──
    if "pattern_type" in df.columns:
        print(f"\n  Pattern breakdown:")
        for pt, grp in df.groupby("pattern_type"):
            grp_closed = grp[grp["exit_reason"] != "OPEN_AT_END"]
            g_wins = grp_closed[grp_closed["pnl"] > 0]
            g_wr = len(g_wins) / len(grp_closed) * 100 if len(grp_closed) > 0 else 0
            g_avg_w = g_wins["pnl"].mean() if len(g_wins) > 0 else 0
            g_pnl = grp_closed["pnl"].sum()
            print(f"    [{pt}] {len(grp_closed):>4} trades | win%={g_wr:.0f}% | "
                  f"avg win ${g_avg_w:+,.0f} | net P&L ${g_pnl:+,.0f}")

    # ── Exit reason breakdown ──
    print(f"\n  Exit reasons:")
    for reason, cnt in df["exit_reason"].value_counts().items():
        pct = cnt / len(df) * 100
        avg_pnl = df[df["exit_reason"] == reason]["pnl"].mean()
        print(f"    {reason:<18}: {cnt:>4} trades ({pct:.0f}%) | avg P&L ${avg_pnl:+,.0f}")

    print("\n" + "═" * 100 + "\n")


# ═══════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(description="VCP Strategy 10-year backtest")
    parser.add_argument("--start",   type=int,   default=2015, help="Start year (default: 2015)")
    parser.add_argument("--end",     type=int,   default=2024, help="End year (default: 2024)")
    parser.add_argument("--capital", type=float, default=10_000, help="Starting capital (default: 10000)")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Custom ticker list")
    parser.add_argument("--universe", choices=["sp100", "growth"], default="sp100",
                        help="Preset universe: sp100 (default) or growth (momentum stocks)")
    args = parser.parse_args()

    if args.tickers:
        tickers = args.tickers
    elif args.universe == "growth":
        tickers = GROWTH_TICKERS
        print("Using GROWTH universe ({} tickers)".format(len(tickers)))
    else:
        tickers = DEFAULT_TICKERS
        print("Using S&P 100 universe ({} tickers)".format(len(tickers)))

    t0 = time.time()

    # 1. Download data
    data = download_data(tickers, args.start, args.end)

    # 2. Run backtest
    trades, equity_curve = run_backtest(
        tickers=[t for t in tickers if t in data],
        data=data,
        start_year=args.start,
        end_year=args.end,
        initial_capital=args.capital,
    )

    # 3. Report
    print_report(
        trades=trades,
        equity_curve=equity_curve,
        spy_df=data["SPY"],
        initial_capital=args.capital,
        start_year=args.start,
        end_year=args.end,
    )

    elapsed = time.time() - t0
    print(f"Total runtime: {elapsed:.0f}s ({elapsed/60:.1f} min)\n")


if __name__ == "__main__":
    main()
