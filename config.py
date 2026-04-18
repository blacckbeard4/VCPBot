"""VCPBot configuration — all constants loaded from .env."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# === Paths ===
BASE_DIR: Path = Path(__file__).parent
DB_PATH: Path = BASE_DIR / "vcpbot.db"
TRADE_LOG_CSV: Path = BASE_DIR / "trade_log.csv"

# === Alpaca ===
ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER: bool = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

# === Market Data Fallbacks ===
FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")
TWELVE_DATA_API_KEY: str = os.getenv("TWELVE_DATA_API_KEY", "")

# === Telegram Alerts ===
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# === Account / Risk ===
ACCOUNT_VALUE: float = float(os.getenv("ACCOUNT_VALUE", "10000"))
MAX_POSITIONS: int = int(os.getenv("MAX_POSITIONS", "5"))
MAX_SECTOR_POSITIONS: int = int(os.getenv("MAX_SECTOR_POSITIONS", "2"))
MAX_DRAWDOWN_PCT: float = float(os.getenv("MAX_DRAWDOWN_PCT", "0.10"))

# ─────────────────────────────────────────────────────────────
# VCP STRATEGY PARAMETERS
# ─────────────────────────────────────────────────────────────

# ── Risk sizing ──
RISK_PCT_NORMAL: float = 0.02       # 2% of equity per trade (normal mode)
RISK_PCT_FTD: float = 0.01          # 1% of equity per trade (FTD early re-entry mode)

# ── Position limits ──
MAX_OPEN_POSITIONS: int = 5
MAX_TOTAL_PORTFOLIO_RISK_PCT: float = 0.10   # 10% combined open risk cap

# ── Entry / exit levels ──
BUY_STOP_OFFSET: float = 0.05       # pivot + $0.05 for buy stop trigger
BUY_LIMIT_BUFFER: float = 0.20      # limit price = trigger + $0.20 (total slippage = $0.25)
TARGET_PCT: float = 0.20            # +20% take profit limit
MAX_STOP_PCT: float = 0.07          # max 7% stop distance below pivot

# ── Universe pre-filters (Phase 2) ──
MIN_AVG_VOLUME: int = 1_000_000     # 50-day avg daily volume minimum
MIN_PRICE: float = 10.0             # minimum close price

# ── Trend template (Phase 3) ──
RS_RANK_PEAK_MIN: int   = 80   # peak RS in trailing 60 days must be >= this
RS_RANK_CURRENT_MIN: int = 70  # current RS at scan time must be >= this
RS_RANK_PREFERRED: int  = 85   # log-only elite tier, no hard filter
MIN_PCT_ABOVE_52W_LOW: float  = 0.30   # close >= 52w_low * 1.30
MAX_PCT_BELOW_52W_HIGH: float = 0.25   # close >= 52w_high * 0.75

# ── VCP pattern (Phase 4) — 3-tier hierarchical pivot detection ──
EARNINGS_BLACKOUT_DAYS: int  = 14    # reject if earnings within 14 days
MIN_BASE_DAYS: int           = 20    # minimum base duration in trading days (≈4 weeks)
MAX_FINAL_CONTRACTION_PCT: float = 0.08   # final contraction depth < 8%
MIN_CONTRACTIONS: int        = 2     # minimum contraction count in base
MAX_CONTRACTIONS: int        = 6     # maximum contraction count to inspect
LSH_MIN_PULLBACK_PCT: float  = 0.15  # LSH must precede >=15% pullback to qualify
PRIOR_UPTREND_MIN_PCT: float = 0.30  # price must have advanced >=30% into the LSH

# Swing pivot parameters — three distinct scales
SWING_MACRO_N: int       = 15   # pass 1: base boundary / Left Side High detection
SWING_CONTRACTION_N: int = 8    # pass 2: intermediate contraction peaks/troughs
SWING_MICRO_N: int       = 3    # pass 3: final tight pivot for exact entry point

# ── High Tight Flag (Phase 4b) ──
HTF_GAIN_8W_MIN_PCT: float     = 100.0  # must gain >=100% in prior 8 weeks (40 bars)
HTF_CONSOLIDATION_MAX_PCT: float = 20.0  # single pullback < 20% from recent high
HTF_DAYS_MIN: int              = 5      # consolidation duration: min bars
HTF_DAYS_MAX: int              = 15     # consolidation duration: max bars

# ── Follow-Through Day (Phase 1) ──
FTD_MIN_DAY: int = 4               # FTD valid starting day 4 of rally attempt
FTD_MAX_DAY: int = 7               # FTD valid through day 7
FTD_MIN_GAIN_PCT: float = 0.015    # index must close up >= 1.5% for FTD
FTD_DISTRIBUTION_WINDOW: int = 3   # distribution day within 3 sessions reverts to CASH

# === Scheduling ===
TIMEZONE: str = "US/Eastern"

# === Retry / Batching ===
YFINANCE_RETRIES: int = 3
YFINANCE_RETRY_SLEEP: int = 60
SCANNER_BATCH_SIZE: int = 50
