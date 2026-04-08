# VCPBot — Claude Code Context

> VCP Momentum Breakout (Minervini-style) swing trading bot.
> Python 3.12, daily timeframe, all liquid US equities, long-only.
> Buy stop-limit bracket orders via Alpaca. No AI calls — pure math.
> Deployed as systemd service on Oracle Cloud VM.

## Deployment
- **VM**: Oracle Cloud VM.Standard.E2.1.Micro (1GB RAM, 1 OCPU)
- **IP**: 150.136.140.21 | **User**: ubuntu
- **Path**: /home/ubuntu/trading-bot
- **Process**: systemd service `trading-bot`
- **Paper trading** by default (`ALPACA_PAPER=true`)
- **SSH key**: `~/.ssh/oracle_trading_bot`
- **Git**: VM is a git repo tracking `origin/main` (https://github.com/blacckbeard4/VCPBot.git)

### Deploy workflow (after any code change)
```bash
# 1. Commit & push locally
git add <file> && git commit -m "..." && git push origin main

# 2. Pull on VM and restart
ssh -i ~/.ssh/oracle_trading_bot ubuntu@150.136.140.21 \
  "cd /home/ubuntu/trading-bot && git pull origin main && sudo systemctl restart trading-bot"

# 3. Tail logs to confirm
ssh -i ~/.ssh/oracle_trading_bot ubuntu@150.136.140.21 \
  "sudo journalctl -u trading-bot -f"
```

## Strategy: VCP Momentum Breakout (Minervini-style)
- **Long-only**, US equities, daily timeframe
- **Signal**: Volatility Contraction Pattern — successive tightening pullbacks
  within an uptrending base, ending with a pocket-pivot breakout above the pivot
- **Entry**: buy stop-limit at `pivot + $0.05` (GTC, triggers intraday if breakout occurs)
- **Stop**: low of the final tight contraction (max 7% below pivot)
- **Target**: `+20%` take-profit limit (linked bracket order on Alpaca's server)
- **Risk**: 2% of equity per trade (normal mode); 1% in FTD early re-entry mode

## Tech Stack
- Python 3.12, yfinance, pandas, alpaca-py, APScheduler,
  pandas_market_calendars, requests, python-dotenv, sqlite3

## File Map
| File | Purpose |
|------|---------|
| main.py | APScheduler entry point, **6 scheduled jobs**, pipeline orchestration |
| config.py | All VCP constants from .env (legacy PullbackBot vars removed) |
| regime.py | Phase 1: SPY+QQQ SMA200 → CASH/FTD/NORMAL, FTD + 3-session distribution window |
| tickers.py | Universe: Alpaca assets API (all active US equities), S&P 500 fallback |
| scanner.py | Phase 2+3: ADV50/price/SMA200 gates + Trend Template + RS Rank + sector cap |
| vcp_detector.py | Phase 4: base duration, earnings blackout, VCP contraction detection |
| risk_manager.py | Phase 5: pure math position sizing, portfolio heat check |
| executor.py | Phase 6: buy stop-limit bracket orders + 10:30 AM RVOL confirmation |
| monitor.py | Phase 7: target/stop checks, fill confirmation, EOD snapshot, expectancy |
| db.py | SQLite schema (5 tables), WAL mode, CSV trade log |
| news.py | Earnings blackout check only (yfinance); news scan removed — unused |
| notifier.py | Telegram alerts (fire-and-forget, never raises) |
| trade_log.csv | CSV of every signal and closed trade (auto-created) |
| CLAUDE.md | This file |
| AGENTS.md | Agent role + decision tree |

## Environment Variables
```
ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER (default true),
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ACCOUNT_VALUE (default 10000),
MAX_POSITIONS (default 5), MAX_SECTOR_POSITIONS (default 2),
MAX_DRAWDOWN_PCT (default 0.10)
```

## VCP Strategy Rules

### Phase 1 — Regime Filter
- SPY close >= SMA200 → NORMAL (2% risk per trade)
- SPY close < SMA200 → CASH_MODE (no new entries, watch for FTD or reclaim)
- Follow-Through Day (FTD): Day 4-7 of rally attempt, SPY or QQQ +1.5% on higher volume
  → FTD_MODE (1% risk per trade until SMA200 reclaimed)
- Distribution day (SPY or QQQ closes down on higher vol) within **3 trading sessions** of FTD → revert to CASH_MODE
- ftd_date persisted to DB — window enforced correctly across restarts
- Rally Day 1 low uses actual intraday Low; if undercut before FTD → reset to Day 0

### Phase 2 — Universe Screen
1. ADV50 (50-day avg daily volume) > 1,000,000
2. Close price > $10
3. Close > 200-day SMA

### Phase 3 — Trend Template + RS Rank + Sector Cap
All 7 must be true:
1. Close > 150 SMA AND Close > 200 SMA
2. 150 SMA > 200 SMA
3. 200 SMA slope positive (today > 30 days ago)
4. 50 SMA > 150 SMA AND 50 SMA > 200 SMA
5. Close > 50 SMA
6. Close >= 52w_low × 1.30 (30% above 52-week low)
7. Close >= 52w_high × 0.75 (within 25% of 52-week high)

RS Rank: RS_Raw = 0.4×ROC_63 + 0.2×ROC_126 + 0.2×ROC_189 + 0.2×ROC_252
Percentile-ranked across full screened universe. **Reject if RS_Rank < 80.**

Sector cap: if a sector already has MAX_SECTOR_POSITIONS open positions, new setups
from that sector are rejected at scan time (not just at order placement).

### Phase 4 — VCP Pattern Detection
1. Base duration >= 4 weeks (52-week high must be >= 4 weeks ago)
2. Next earnings > 14 calendar days away
3. 2-4 contractions, each shallower than the last (tightening)
4. Final contraction < 8% depth (high to low)
5. Volume dry-up during final contraction (avg < 50-day avg volume)
6. Pivot = highest high in the final contraction range
7. Stop = low of the final contraction (max 7% below **pivot** — reject if wider)

### Phase 5 — Risk Calculation
- `risk_per_trade = account_equity × risk_pct`
  - Normal: 2% | FTD mode: 1%
- `risk_unit = entry_price - stop_price`
- `position_size = risk_per_trade / risk_unit` (fractional shares supported)
- Hard cap: stop must not exceed 7% below **pivot** → reject
- Max 5 simultaneous open positions
- Total combined portfolio risk cap: 10% of equity

### Phase 6 — Execution
- At 9:30 AM: place GTC buy stop-limit bracket orders for all PENDING setups
- Trigger price = pivot + $0.05
- Limit price = trigger + $0.20 = pivot + $0.25 (total slippage buffer)
- Linked stop loss at stop_loss_price (sits on Alpaca's server)
- Linked take profit limit at entry × 1.20
- If price already > pivot + 2% at 9:30 AM → cancel (GAP_CANCELLED)
- **At 10:30 AM**: RVOL check — if actual vol < adv50 × (60/390) × 1.5 → cancel (RVOL_CANCELLED)

### Phase 7 — Trade Management
- Monitor stop hits (intraday + EOD) — Alpaca's stop order fires automatically
- Monitor +20% target hits — Alpaca's take-profit limit fires automatically
- Monitor module confirms order status and updates DB/CSV
- Cancel buy stop orders that haven't triggered within 1 trading day
- After every trade close: auto-recalculate win_rate, avg_win, avg_loss, expectancy → logged + sent in Telegram alert

## SQLite Schema (vcpbot.db) — 5 tables
- **trades**: id, ticker, direction(LONG), entry_price, stop_price, target_1_price,
  shares, entry_date, status, exit_price, exit_date, exit_reason, pnl, r_multiple,
  pivot_price, rs_rank, base_duration_weeks, contraction_depth_pct, alpaca_order_id, ftd_date
- **scan_log**: run_date, regime, tickers_scanned, tickers_phase2, tickers_phase3,
  vcp_setups, orders_queued
- **portfolio_state**: daily snapshot with drawdown tracking
- **regime_state**: persisted Cash/FTD mode state + ftd_date across restarts
- **errors**: step-level error logging

## Trade Status Flow
```
PENDING → PLACED (order on Alpaca) → OPEN (filled) → STOPPED | TARGET_HIT
       → GAP_CANCELLED (price gapped above pivot at 9:30 AM)
       → RVOL_CANCELLED (intraday volume < 1.5x expected pace at 10:30 AM)
       → CANCELLED (order not placed / stale)
       → EXPIRED (buy stop not triggered in 1 day)
```

## Scheduling (all US/Eastern)
- **4:05 PM Mon-Fri**: `run_scan_pipeline()` (Phases 1-5)
- **9:30 AM Mon-Fri**: `run_execution_job()` (Phase 6 — place orders)
- **10:30 AM Mon-Fri**: `run_rvol_check_job()` (Phase 6 — RVOL confirmation)
- **Every 30min 9am-4pm**: `run_intraday_monitor()` (Phase 7)
- **4:05 PM Mon-Fri**: `run_eod_monitor_job()` (Phase 7 EOD)
- **Sunday 8:00 AM**: `run_weekly_report_job()`

## How to Run
```bash
cd /home/ubuntu/trading-bot
source venv/bin/activate
python main.py                   # production scheduler
python main.py --dry-run         # full pipeline, no orders placed
python main.py --run-now         # run scan immediately and exit (for testing)
sudo systemctl start trading-bot # background service
sudo journalctl -u trading-bot -f
```

## Known Gotchas
- 1GB RAM: batch yfinance downloads (50 tickers), gc.collect() after each batch
- yfinance rate limits: 2s sleep between batches, 3 retries on failure
- Alpaca paper vs live: ALPACA_PAPER=true by default — explicit change required
- APScheduler: always use explicit timezone, never naive datetimes
- SQLite WAL mode for concurrent reads, timeout=10 for lock contention
- Fractional shares: position_size uses round(x, 2) — Alpaca paper supports this
- FTD logic uses day-over-day close/volume (no intraday data) — approximation
- VCP swing detection uses n=3 bars each side — tune SWING_PIVOT_BARS in config.py
- Sector lookup in scanner uses yfinance LRU cache (maxsize=2048) — ~1s per new ticker

## Build Status
| File | Status | Notes |
|------|--------|-------|
| config.py | DONE | VCP constants only — all legacy PullbackBot vars removed |
| tickers.py | DONE | Alpaca assets API + S&P 500 fallback; SECTOR_MAP removed |
| db.py | DONE | 5 tables, VCP columns, CSV trade log, ftd_date migration |
| regime.py | DONE | SPY+QQQ, Cash/FTD/Normal, 3-session distribution window, Day1 intraday low |
| scanner.py | DONE | Phase 2+3 + RS rank + sector cap at scan time |
| vcp_detector.py | DONE | Phase 4: base/earnings/contractions/volume/pivot |
| risk_manager.py | DONE | Phase 5: pure math, 2%/1% risk, stop_pct vs pivot (fixed) |
| executor.py | DONE | Phase 6: buy stop-limit bracket + 10:30 AM RVOL check |
| monitor.py | DONE | Phase 7: target/stop checks, EOD snapshot, expectancy on close |
| news.py | DONE | Earnings blackout only (news scan removed — was unused) |
| notifier.py | DONE | VCP-specific alerts with expectancy block on trade close |
| main.py | DONE | 6 scheduled jobs, --dry-run, --run-now |
| requirements.txt | DONE | openai removed |
| CLAUDE.md | DONE | This file — updated 2026-04-05 |
| AGENTS.md | DONE | Agent instructions updated |

## SESSION HANDOFF
**Last updated**: 2026-04-05 — Full QC pass + audit complete.

Changes applied today:
1. Fix 1: regime.py — post-FTD 3-session distribution window + QQQ check + Day1 intraday low
2. Fix 2: executor.py — 10:30 AM RVOL < 1.5x cancellation
3. Verify 1: scanner.py — MAX_SECTOR_POSITIONS enforced at scan time
4. Verify 2: monitor.py/notifier.py — expectancy auto-calc on every trade close
5. QC: risk_manager.py — stop_pct denominator fixed (pivot, not entry_price)
6. QC: config.py — 10 dead legacy vars removed
7. QC: news.py — dead check_news() block removed
8. QC: tickers.py — dead SECTOR_MAP / get_sector() removed
9. Audit: pullbackbot.db, analyst.py, planner.py, backtest.py, 2 empty CSVs deleted
