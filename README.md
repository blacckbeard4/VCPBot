# VCPBot — VCP Momentum Breakout Trading Bot

A fully automated swing trading bot implementing **Mark Minervini's Volatility Contraction Pattern (VCP)** strategy on US equities. Long-only, daily timeframe, bracket orders via [Alpaca](https://alpaca.markets/). Deployed as a systemd service on Oracle Cloud.

> **Paper trading by default** — no real money at risk until you flip `ALPACA_PAPER=false`.

---

## Strategy Overview

VCP (Volatility Contraction Pattern) is a price structure where a stock forms a base with **successive tightening pullbacks** — each contraction shallower than the last — culminating in a pocket-pivot breakout above the pivot high on rising volume.

```
  PRICE
    │
    │  - - - - - - - - - - - - - - - - - - - - - - - - - -   🎯 +20% TARGET
    │                                                    /
    │  ══════════════════════════════════════════════  /   ← BUY STOP ENTRY
    │  /\                      /\          /\        /        (pivot + $0.05)
    │ /  \                    /  \        /  \      /    ← Breakout on HIGH VOLUME
    │/    \                  /    \      /    \    /
    │      \                /      \    /      \  /      } RISK ≤ 7%
    │       \              /   C2   \  /   C3   \/- - - - STOP LOSS (low of C3)
    │        \            /  ~15%   \/  <8% deep
    │   C1    \          /
    │  ~25%    \        /
    │           \      /
    │            \    /
    │             \  /
    │              \/
    │
    │◄──────────── BASE ≥ 4 WEEKS ───────────────►│
    └────────────────────────────────────────────────────────► TIME

  VOLUME
    │ █                                                █ █  ← surge at breakout
    │ █  █                                          █  █ █
    │ █  █  █     █  █           █  █        █  █  ██  █ █
    │ █  █  █  █  █  █  █  █  █ █  █  █  █  █  █  ██  █ █
    └───────────── drying up through base ─────────────────────► TIME
```

**Risk/Reward per trade:**

```
    ┌──────────────────────────────────────────┐
    │  🎯  TARGET +20%                         │
    │       /                                  │
    │      /   REWARD = 20%                    │
    │     /                                    │
    │────/──── ENTRY (pivot + $0.05) ──────────│  ← buy stop triggers here
    │    \                                     │
    │     \   RISK ≤ 7%                        │
    │      \                                   │
    │  🛑   STOP LOSS (low of final contraction)│
    │                                          │
    │  Risk : Reward  =  1 : 2.8+             │
    │  Sized to risk 2% of account equity      │
    └──────────────────────────────────────────┘
```

---

## Architecture

### 7-Phase Pipeline (runs 4:05 PM daily after market close)

```mermaid
flowchart TD
    A([4:05 PM Mon-Fri]) --> P1

    P1["Phase 1 — Regime Filter\nregime.py\nSPY/QQQ vs SMA200"]
    P1 -->|CASH MODE| STOP1([No new entries\nWatch for FTD])
    P1 -->|NORMAL / FTD MODE| P2

    P2["Phase 2 — Universe Screen\ntickers.py + scanner.py\nAll active US equities\n▸ ADV50 > 1M shares\n▸ Close > $10\n▸ Close > SMA200"]
    P2 --> P3

    P3["Phase 3 — Trend Template + RS Rank\nscanner.py\n▸ 7-condition trend template\n▸ RS Rank ≥ 80th percentile\n▸ Sector cap enforcement"]
    P3 --> P4

    P4["Phase 4 — VCP Detection\nvcp_detector.py\n▸ Base ≥ 4 weeks\n▸ Earnings blackout check\n▸ 2–4 tightening contractions\n▸ Final contraction < 8%\n▸ Volume dry-up confirmed\n▸ Pivot + stop identified"]
    P4 --> P5

    P5["Phase 5 — Risk Sizing\nrisk_manager.py\n▸ 2% risk/trade (Normal)\n▸ 1% risk/trade (FTD mode)\n▸ Stop ≤ 7% below pivot\n▸ Max 5 positions\n▸ Max 10% portfolio risk"]
    P5 --> DB[(SQLite DB\nPENDING trades)]

    DB --> P6

    P6["Phase 6 — Execution\nexecutor.py\n9:30 AM: place GTC bracket orders\n10:30 AM: RVOL confirmation check"]
    P6 -->|Gap > 2% at open| GAP([GAP_CANCELLED])
    P6 -->|Vol < 1.5× expected| RVOL([RVOL_CANCELLED])
    P6 -->|Order placed| P7

    P7["Phase 7 — Trade Management\nmonitor.py\nEvery 30 min + EOD\n▸ Stop/target hit detection\n▸ Fill confirmation\n▸ Stale order cancellation\n▸ Expectancy tracking"]
    P7 -->|Stop hit| STOPPED([STOPPED])
    P7 -->|+20% reached| TARGET([TARGET_HIT])

    style P1 fill:#4a6fa5,color:#fff
    style P2 fill:#4a6fa5,color:#fff
    style P3 fill:#4a6fa5,color:#fff
    style P4 fill:#6b8e5e,color:#fff
    style P5 fill:#6b8e5e,color:#fff
    style P6 fill:#8e6b3e,color:#fff
    style P7 fill:#8e6b3e,color:#fff
    style STOP1 fill:#888,color:#fff
    style GAP fill:#c0392b,color:#fff
    style RVOL fill:#c0392b,color:#fff
    style STOPPED fill:#c0392b,color:#fff
    style TARGET fill:#27ae60,color:#fff
```

---

### Daily Schedule (US/Eastern)

```mermaid
gantt
    title VCPBot Daily Schedule (Mon–Fri)
    dateFormat HH:mm
    axisFormat %H:%M

    section Pre-Market
    Market Closed        :done, 00:00, 09:30

    section Market Hours
    Phase 6 — Place Orders         :crit, 09:30, 09:31
    RVOL Confirmation Check        :crit, 10:30, 10:31
    Phase 7 — Intraday Monitor ×13 :active, 09:30, 16:00

    section After Close
    Phase 1-5 Scan Pipeline        :crit, 16:05, 16:35
    Phase 7 — EOD Monitor          :16:05, 16:20
```

---

### Regime State Machine

```mermaid
stateDiagram-v2
    [*] --> NORMAL : SPY ≥ SMA200

    NORMAL --> CASH : SPY closes below SMA200
    CASH --> NORMAL : SPY reclaims SMA200

    CASH --> FTD : Day 4–7 rally attempt\nSPY or QQQ +1.5% on higher vol
    FTD --> NORMAL : SPY reclaims SMA200
    FTD --> CASH : Distribution day within 3 sessions

    note right of NORMAL : 2% risk per trade
    note right of FTD    : 1% risk per trade\n(early re-entry)
    note right of CASH   : No new entries
```

---

### Trade Status Flow

```mermaid
stateDiagram-v2
    [*] --> PENDING : VCP setup approved\n(Phase 5)

    PENDING --> PLACED       : Buy stop order submitted\n(9:30 AM)
    PENDING --> GAP_CANCELLED : Price gapped >2% above pivot\nat open
    PLACED  --> RVOL_CANCELLED : Intraday vol < 1.5× expected\n(10:30 AM check)
    PLACED  --> OPEN         : Order filled (breakout triggered)
    PLACED  --> EXPIRED      : Buy stop not triggered in 1 day
    PLACED  --> CANCELLED    : Manual / other cancellation

    OPEN --> STOPPED    : Stop loss hit\n(Alpaca server-side order)
    OPEN --> TARGET_HIT : +20% take-profit hit\n(Alpaca server-side order)
```

---

### Component Map

```mermaid
graph LR
    subgraph Orchestration
        main["main.py\nAPScheduler\n6 cron jobs"]
    end

    subgraph Data
        yf["yfinance\nPrice/volume data"]
        alpaca_api["Alpaca API\nOrders + Portfolio"]
        tickers["tickers.py\nUniverse builder"]
    end

    subgraph Pipeline
        regime["regime.py\nPhase 1"]
        scanner["scanner.py\nPhases 2+3"]
        vcp["vcp_detector.py\nPhase 4"]
        risk["risk_manager.py\nPhase 5"]
        executor["executor.py\nPhase 6"]
        monitor["monitor.py\nPhase 7"]
    end

    subgraph Storage
        db["db.py\nSQLite (WAL)\n5 tables"]
        csv["trade_log.csv\nTrade history"]
    end

    subgraph Alerts
        notifier["notifier.py\nTelegram"]
    end

    main --> regime
    main --> scanner
    main --> vcp
    main --> risk
    main --> executor
    main --> monitor

    tickers --> yf
    scanner --> yf
    vcp --> yf
    regime --> yf
    executor --> alpaca_api
    monitor --> alpaca_api

    risk --> db
    executor --> db
    monitor --> db
    db --> csv

    monitor --> notifier
    executor --> notifier
    regime --> notifier
```

---

## Setup

### 1. Clone & install

```bash
git clone https://github.com/blacckbeard4/VCPBot.git
cd VCPBot
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

```env
ALPACA_API_KEY=your_key
ALPACA_SECRET_KEY=your_secret
ALPACA_PAPER=true              # set false for live trading

TELEGRAM_BOT_TOKEN=your_token
TELEGRAM_CHAT_ID=your_chat_id

ACCOUNT_VALUE=10000            # starting account size
MAX_POSITIONS=5
MAX_SECTOR_POSITIONS=2
MAX_DRAWDOWN_PCT=0.10
```

### 3. Run

```bash
# Test the full pipeline without placing any orders
python main.py --dry-run

# Run scan once and exit (useful for debugging)
python main.py --run-now

# Production scheduler
python main.py
```

---

## Deployment (Oracle Cloud VM)

```bash
# VM: Oracle Cloud VM.Standard.E2.1.Micro — 1GB RAM / 1 OCPU
# Ubuntu, 1GB RAM — memory-conscious batched processing

# Install as systemd service
sudo cp trading-bot.service /etc/systemd/system/
sudo systemctl enable trading-bot
sudo systemctl start trading-bot

# Logs
sudo journalctl -u trading-bot -f
```

---

## Key Strategy Parameters

| Parameter | Value | Description |
|---|---|---|
| Risk per trade (Normal) | 2% | % of equity risked |
| Risk per trade (FTD mode) | 1% | Reduced during early market recovery |
| Stop distance max | 7% below pivot | Hard reject if wider |
| Take-profit target | +20% | Linked bracket order on Alpaca |
| Entry trigger | pivot + $0.05 | GTC buy stop |
| Entry limit | pivot + $0.25 | Slippage buffer |
| Min base duration | 4 weeks | VCP base requirement |
| Final contraction max | 8% | Tightest squeeze |
| Min contractions | 2 | Need evidence of tightening |
| RS Rank minimum | 80th percentile | Top 20% relative strength |
| ADV50 minimum | 1,000,000 shares | Liquidity filter |
| Min price | $10 | Penny stock filter |

---

## SQLite Schema

```
vcpbot.db
├── trades           — every setup: PENDING → OPEN → STOPPED/TARGET_HIT
├── scan_log         — daily pipeline run stats
├── portfolio_state  — daily NAV + drawdown snapshots
├── regime_state     — persisted Cash/FTD state (survives restarts)
└── errors           — step-level error log
```

---

## Tech Stack

- **Python 3.12** — runtime
- **yfinance** — OHLCV price data
- **alpaca-py** — brokerage API (orders, portfolio)
- **APScheduler** — cron-style job scheduling
- **pandas / pandas-market-calendars** — data processing + NYSE calendar
- **SQLite (WAL mode)** — persistence
- **Telegram Bot API** — trade alerts

---

## Disclaimer

This software is for **educational and research purposes only**. It is not financial advice. Trading involves substantial risk of loss. Use paper trading mode until you fully understand the system behavior.
