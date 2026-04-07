# VCPBot — Agent Context (AGENTS.md)

## Bot Overview
- **Name**: VCPBot — VCP Momentum Breakout (Minervini-style)
- **Strategy**: Long-only, US equities, daily timeframe
- **Broker**: Alpaca (paper trading by default)
- **DB**: SQLite `vcpbot.db` (5 tables)
- **Alerts**: Telegram Bot API
- **No AI calls** — pure math risk management

## Agent Role

The agent acts as an **end-of-day scanner + next-morning executor**:

1. **After market close (4:05 PM EST)**: Run the full scan pipeline to identify VCP setups
2. **Next morning (9:30 AM EST)**: Place buy stop-limit bracket orders for approved setups
3. **Intraday / EOD**: Monitor open positions, confirm fills, cancel stale orders

---

## Decision Tree

```
Every market day at 4:05 PM:

1. REGIME CHECK (regime.py)
   ├─ SPY >= SMA200? → NORMAL MODE (2% risk per trade)
   ├─ SPY < SMA200?  → CASH MODE
   │   ├─ Watch for FTD (Day 4-7 of rally, +1.5% on higher vol)
   │   │   └─ FTD fired? → FTD MODE (1% risk per trade)
   │   └─ SPY reclaims SMA200? → back to NORMAL MODE
   └─ In CASH MODE: STOP — no new entries, log and exit

2. UNIVERSE SCREEN (scanner.py Phase 2)
   ├─ ADV50 > 1,000,000 shares
   ├─ Close > $10
   └─ Close > 200-day SMA

3. TREND TEMPLATE (scanner.py Phase 3) — ALL 7 must pass:
   ├─ Close > 150 SMA and Close > 200 SMA
   ├─ 150 SMA > 200 SMA
   ├─ 200 SMA slope > 0 (30-day trend)
   ├─ 50 SMA > 150 SMA and 50 SMA > 200 SMA
   ├─ Close > 50 SMA
   ├─ Close >= 52w_low × 1.30
   └─ Close >= 52w_high × 0.75

4. RS RANK FILTER (scanner.py)
   └─ RS_Rank < 80? → REJECT (only top 20% by relative strength proceed)

5. VCP PATTERN DETECTION (vcp_detector.py) — ALL must pass:
   ├─ Base duration >= 4 weeks
   ├─ Earnings >= 14 days away
   ├─ 2-4 contractions, each tighter than the last
   ├─ Final contraction < 8%
   ├─ Volume dry-up in final contraction (avg vol < 50-day avg)
   └─ Stop distance <= 7% of pivot → if wider, REJECT

6. RISK CALCULATION (risk_manager.py)
   ├─ shares = (account × risk_pct) / risk_unit
   ├─ Max 5 open positions → queue if at limit
   └─ Portfolio heat > 10% → queue if would exceed

7. QUEUE ORDER (db.py status = PENDING)
   └─ Next morning at 9:30 AM → executor.py places buy stop-limit bracket order
```

---

## Rules the Agent Must Never Break

1. **Never enter a trade without a confirmed VCP pivot, volume dry-up, and a calculable stop**
2. **Never risk more than 2% of equity per trade** (1% in FTD mode)
3. **Never hold more than 5 positions simultaneously**
4. **In CASH MODE, the agent's only job is to monitor SPY for FTD or SMA200 reclaim — nothing else**
5. **Never enter a stock within 14 days of earnings**
6. **Never place an order where the stop is more than 7% below the pivot**
7. **Log every decision** — approvals AND rejections with reasons — to DB and CSV

---

## Logging Requirements

Every decision must be logged with:
- **Approval**: ticker, pivot, stop, target, shares, risk%, RS_Rank, base_weeks, contraction_depths
- **Rejection**: ticker, rejection reason (which phase failed and why)
- **Trade entry**: to DB (trades table) and CSV trade_log.csv
- **Trade exit**: to DB (update status, pnl, r_multiple) and CSV
- **Pipeline run**: to scan_log table with counts per phase

---

## Performance Tracking

`trade_log.csv` records every signal and closed trade:
- Signal columns: date, ticker, entry_price, stop_price, target_price, shares,
  account_equity_at_entry, rs_rank, base_weeks, contraction_depth_pct
- Exit columns (appended at close): exit_date, exit_price, exit_reason,
  pnl_dollars, pnl_pct, r_multiple

`db.compute_stats_from_csv()` recalculates after every closed trade:
  win_rate, avg_win, avg_loss, expectancy

---

## File Map + Build Status

| File | Status | Notes |
|------|--------|-------|
| config.py | DONE | VCP constants, env vars preserved |
| tickers.py | DONE | Alpaca assets API + S&P 500 fallback |
| db.py | DONE | 5 tables, VCP columns, CSV trade log |
| regime.py | DONE | Cash/FTD/Normal + FTD detection |
| scanner.py | DONE | Phase 2+3 |
| vcp_detector.py | DONE | Phase 4 |
| risk_manager.py | DONE | Phase 5 — pure math |
| executor.py | DONE | Phase 6 — buy stop-limit |
| monitor.py | DONE | Phase 7 |
| news.py | DONE | Earnings + news keywords |
| notifier.py | DONE | VCP-specific Telegram alerts |
| main.py | DONE | 5 jobs + --dry-run + --run-now |
| requirements.txt | DONE | openai removed |

## Deprecated / Unused Files (left in place, not called)
| File | Status |
|------|--------|
| analyst.py | DEPRECATED — old Claude per-ticker analysis, not called |
| planner.py | DEPRECATED — old entry/stop math, replaced by vcp_detector + risk_manager |
| backtest.py | NOT UPDATED — old backtester, incompatible with new schema |

---

## Environment Variable Names (unchanged from PullbackBot)
```
ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER,
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ACCOUNT_VALUE,
RISK_PCT_PER_TRADE, MAX_POSITIONS, MAX_SECTOR_POSITIONS,
MAX_DRAWDOWN_PCT, GAP_OPEN_THRESHOLD, MIN_PULLBACK_DEPTH,
MIXED_REGIME_SIZE_MULTIPLIER,
AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT
(Azure keys kept for .env compatibility — not used by VCP strategy)
```

---

## TASK QUEUE

- [x] Rewrite config.py with VCP constants
- [x] Rewrite tickers.py (Alpaca assets API)
- [x] Rewrite db.py (VCP schema, CSV log)
- [x] Rewrite regime.py (Cash/FTD/Normal)
- [x] Rewrite scanner.py (Phase 2+3)
- [x] Create vcp_detector.py (Phase 4)
- [x] Rewrite risk_manager.py (Phase 5, no AI)
- [x] Rewrite executor.py (buy stop-limit bracket)
- [x] Rewrite monitor.py (Phase 7)
- [x] Rewrite news.py (earnings + keywords)
- [x] Rewrite notifier.py (VCP alerts)
- [x] Rewrite main.py (--dry-run, --run-now)
- [x] Update CLAUDE.md
- [x] Update AGENTS.md
- [ ] Run: `python main.py --dry-run --run-now` to smoke test
- [ ] Deploy to Oracle VM

## SESSION HANDOFF
**Last updated**: 2026-04-05 — Full strategy rewrite complete.

All 14 strategy files replaced. VCPBot (Minervini-style VCP momentum breakout)
now replaces PullbackBot. Alpaca connection and paper trading setup unchanged.

Next: smoke test with `python main.py --dry-run --run-now`, then deploy.
