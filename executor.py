"""VCPBot Phase 6 — Execution: place buy stop-limit bracket orders via Alpaca.

At 9:30 AM for each PENDING setup:
  1. Get current price from Alpaca
  2. If price already above pivot + 2%: cancel (gapped too far)
  3. Place a GTC buy stop-limit bracket order:
     - Trigger: pivot_price + $0.05
     - Limit:   pivot_price + $0.25 (slippage buffer)
     - Stop loss: stop_loss_price (linked, on Alpaca's server)
     - Take profit: entry_approx * 1.20 (linked, on Alpaca's server)
  4. Update DB: PENDING → PLACED (order sitting on Alpaca)

At 10:30 AM for each PLACED order:
  - Fetch intraday volume traded 9:30–10:30 AM via Alpaca bars API
  - Compare to expected volume: adv50 * (60min / 390min session)
  - If actual < expected * 1.5 → cancel order, status = RVOL_CANCELLED
  - If actual >= expected * 1.5 → log confirmation, order stays live

Also provides:
  - check_placed_orders(): poll Alpaca for fill status of PLACED orders
  - cancel_stale_orders(): cancel orders that haven't triggered after 1 day
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    GetOrdersRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, OrderStatus

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER,
    BUY_STOP_OFFSET, BUY_LIMIT_BUFFER, TARGET_PCT, TIMEZONE,
)
import db
from notifier import (
    send_trade_alert, send_fill_alert, send_gap_cancel_alert, send_error_alert,
)

logger = logging.getLogger(__name__)
ET = ZoneInfo(TIMEZONE)

_client: Optional[TradingClient] = None


def _get_client() -> TradingClient:
    """Lazy singleton Alpaca TradingClient."""
    global _client
    if _client is None:
        _client = TradingClient(
            ALPACA_API_KEY, ALPACA_SECRET_KEY,
            paper=ALPACA_PAPER,
        )
    return _client


def get_portfolio_value() -> Optional[float]:
    """Fetch live portfolio_value from Alpaca (cash + open position value)."""
    try:
        account = _get_client().get_account()
        return float(account.portfolio_value)
    except Exception as e:
        logger.warning("Could not fetch portfolio value: %s", e)
        return None


def get_current_price(ticker: str) -> Optional[float]:
    """Get latest trade price via Alpaca data API."""
    try:
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest

        data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        request = StockLatestTradeRequest(symbol_or_symbols=[ticker])
        trades = data_client.get_stock_latest_trade(request)
        if ticker in trades:
            return float(trades[ticker].price)
        return None
    except Exception as e:
        logger.warning("Failed to get price for %s: %s", ticker, e)
        return None


def place_buy_stop_bracket(
    ticker: str,
    shares: float,
    pivot_price: float,
    stop_loss_price: float,
    dry_run: bool = False,
) -> Optional[str]:
    """Submit a GTC buy stop-limit bracket order via Alpaca.

    Order structure:
      Parent:      BUY stop-limit (trigger=pivot+0.05, limit=pivot+0.25)
      Leg 1:       Stop loss at stop_loss_price
      Leg 2:       Take profit limit at (pivot+0.05) * 1.20

    Returns Alpaca order_id or None on failure.
    On dry_run: logs the order but does not submit it, returns "DRY_RUN".
    """
    trigger_price = round(pivot_price + BUY_STOP_OFFSET, 2)
    limit_price = round(pivot_price + BUY_STOP_OFFSET + BUY_LIMIT_BUFFER, 2)
    take_profit_price = round(trigger_price * (1 + TARGET_PCT), 2)

    if dry_run:
        logger.info(
            "[DRY RUN] Would place buy stop-limit bracket for %s: "
            "trigger=%.2f limit=%.2f stop=%.2f target=%.2f shares=%.2f",
            ticker, trigger_price, limit_price, stop_loss_price, take_profit_price, shares,
        )
        return "DRY_RUN"

    try:
        import requests as _requests

        base_url = (
            "https://paper-api.alpaca.markets"
            if ALPACA_PAPER
            else "https://api.alpaca.markets"
        )
        headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        order_data = {
            "symbol": ticker,
            "qty": str(shares),
            "side": "buy",
            "type": "stop_limit",
            "time_in_force": "gtc",
            "stop_price": str(trigger_price),
            "limit_price": str(limit_price),
            "order_class": "bracket",
            "stop_loss": {"stop_price": str(round(stop_loss_price, 2))},
            "take_profit": {"limit_price": str(take_profit_price)},
        }

        resp = _requests.post(
            f"{base_url}/v2/orders",
            json=order_data,
            headers=headers,
            timeout=15,
        )

        if resp.status_code in (200, 201):
            order_json = resp.json()
            order_id = order_json.get("id", "")
            logger.info(
                "Placed bracket order for %s: trigger=%.2f limit=%.2f "
                "stop=%.2f target=%.2f shares=%.2f order_id=%s",
                ticker, trigger_price, limit_price, stop_loss_price,
                take_profit_price, shares, order_id,
            )
            return order_id
        else:
            logger.error("Alpaca order failed for %s: %s %s",
                         ticker, resp.status_code, resp.text)
            return None

    except Exception as e:
        logger.error("Failed to place order for %s: %s", ticker, e)
        send_error_alert("executor", f"Order failed for {ticker}: {e}")
        return None


def cancel_order(order_id: str) -> bool:
    """Cancel an Alpaca order. Returns True if successful."""
    try:
        _get_client().cancel_order_by_id(order_id)
        logger.info("Cancelled order %s", order_id)
        return True
    except Exception as e:
        logger.error("Failed to cancel order %s: %s", order_id, e)
        return False


def run_execution(dry_run: bool = False) -> None:
    """9:30 AM execution: place buy stop-limit orders for all PENDING setups."""
    pending = db.get_pending_trades()
    if not pending:
        logger.info("No pending trades to execute")
        return

    logger.info("Placing orders for %d pending setups", len(pending))
    placed = 0
    cancelled = 0

    for trade in pending:
        trade_id = trade["id"]
        ticker = trade["ticker"]
        pivot = trade["pivot_price"] or trade["entry_price"]
        stop = trade["stop_price"]
        target = trade["target_1_price"]
        shares = trade["shares"]

        if not pivot:
            logger.warning("No pivot price for trade %d (%s) — skipping", trade_id, ticker)
            db.update_trade_status(trade_id, "CANCELLED",
                                   exit_date=datetime.now(ET).isoformat(),
                                   exit_reason="Missing pivot price")
            cancelled += 1
            continue

        # Pre-flight: check if price already gapped way above pivot
        current_price = get_current_price(ticker)
        if current_price is not None:
            gap_from_pivot = (current_price - pivot) / pivot
            if gap_from_pivot > 0.02:  # price already 2%+ above pivot
                logger.info("GAP: %s current=%.2f pivot=%.2f (%.1f%% above) — cancelling",
                             ticker, current_price, pivot, gap_from_pivot * 100)
                db.update_trade_status(trade_id, "GAP_CANCELLED",
                                       exit_date=datetime.now(ET).isoformat(),
                                       exit_reason="Price gapped above pivot before order placed")
                send_gap_cancel_alert(ticker, pivot, current_price)
                cancelled += 1
                continue

        order_id = place_buy_stop_bracket(ticker, shares, pivot, stop, dry_run=dry_run)
        if order_id:
            status = "PLACED" if order_id != "DRY_RUN" else "PENDING"
            db.update_trade_status(
                trade_id, status,
                alpaca_order_id=order_id,
            )
            send_trade_alert(
                ticker=ticker,
                shares=shares,
                pivot=pivot,
                stop=stop,
                target=target,
                risk_pct=abs(pivot - stop) / pivot if pivot else 0,
            )
            placed += 1
        else:
            db.update_trade_status(trade_id, "CANCELLED",
                                   exit_date=datetime.now(ET).isoformat(),
                                   exit_reason="Order placement failed")
            cancelled += 1

    logger.info("Execution complete: %d placed, %d cancelled", placed, cancelled)


def check_placed_orders() -> None:
    """Poll Alpaca for fill status of PLACED orders. Update DB accordingly.

    - FILLED → update to OPEN, record fill price
    - CANCELLED / EXPIRED → update status
    """
    placed = db.get_placed_trades()
    if not placed:
        return

    try:
        client = _get_client()
    except Exception as e:
        logger.warning("Cannot connect to Alpaca for order check: %s", e)
        return

    for trade in placed:
        trade_id = trade["id"]
        ticker = trade["ticker"]
        order_id = trade["alpaca_order_id"]
        if not order_id:
            continue

        try:
            order = client.get_order_by_id(order_id)
            status = str(order.status).lower()

            if status == "filled":
                fill_price = float(order.filled_avg_price or order.limit_price or 0)
                db.update_trade_status(
                    trade_id, "OPEN",
                    entry_price=fill_price,
                    entry_date=datetime.now(ET).strftime("%Y-%m-%d"),
                )
                send_fill_alert(
                    ticker=ticker,
                    shares=float(trade["shares"]),
                    fill_price=fill_price,
                    stop=float(trade["stop_price"]),
                    target=float(trade["target_1_price"]),
                )
                logger.info("Order filled: %s @ %.2f", ticker, fill_price)

            elif status in ("cancelled", "expired"):
                db.update_trade_status(
                    trade_id, "CANCELLED",
                    exit_date=datetime.now(ET).isoformat(),
                    exit_reason=f"Alpaca order {status}",
                )
                logger.info("Order %s for %s", status, ticker)

        except Exception as e:
            logger.warning("Failed to check order %s for %s: %s", order_id, ticker, e)


def cancel_stale_orders(max_days: int = 1) -> None:
    """Cancel and mark as expired any PLACED orders older than max_days."""
    placed = db.get_placed_trades()
    today = datetime.now(ET).date()

    for trade in placed:
        try:
            entry_date = datetime.fromisoformat(trade["entry_date"]).date()
            age_days = (today - entry_date).days
            if age_days >= max_days:
                order_id = trade["alpaca_order_id"]
                if order_id and order_id != "DRY_RUN":
                    cancel_order(order_id)
                db.update_trade_status(
                    trade["id"], "EXPIRED",
                    exit_date=datetime.now(ET).isoformat(),
                    exit_reason=f"Not triggered after {age_days}d",
                )
                logger.info("Cancelled stale order for %s (age=%dd)", trade["ticker"], age_days)
        except Exception as e:
            logger.warning("Error cancelling stale order for %s: %s", trade["ticker"], e)


# ─── RVOL confirmation (10:30 AM check) ─────────────────────

_SESSION_MINUTES = 390.0   # 6.5 hour NYSE session in minutes
_RVOL_CHECK_ELAPSED = 60.0  # minutes from open to the 10:30 AM check
_RVOL_THRESHOLD = 1.5       # require 1.5x expected pace


def _fetch_intraday_volume(ticker: str) -> Optional[float]:
    """Fetch cumulative intraday volume for ticker from 9:30 to ~10:30 AM via Alpaca bars.

    Returns total shares traded, or None on failure.
    """
    try:
        from alpaca.data.historical.stock import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

        # Build a window: today 9:30 AM → 10:35 AM ET (small buffer)
        now_et = datetime.now(ET)
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        check_time = now_et.replace(hour=10, minute=35, second=0, microsecond=0)

        request = StockBarsRequest(
            symbol_or_symbols=[ticker],
            timeframe=TimeFrame.Minute,
            start=market_open.astimezone(timezone.utc),
            end=check_time.astimezone(timezone.utc),
        )
        bars = data_client.get_stock_bars(request)
        # BarSet doesn't support `in` operator — access directly
        try:
            bars_list = bars[ticker]
        except (KeyError, Exception):
            bars_list = None
        if not bars_list:
            return None

        total_vol = sum(float(bar.volume) for bar in bars_list)
        return total_vol

    except Exception as e:
        logger.warning("Failed to fetch intraday bars for %s: %s", ticker, e)
        return None


def _fetch_adv50_yfinance(ticker: str) -> Optional[float]:
    """Fetch the 50-day average daily volume for a ticker via yfinance."""
    try:
        import yfinance as yf
        df = yf.download(ticker, period="3mo", interval="1d",
                         progress=False, threads=False, auto_adjust=True)
        if df is None or len(df) < 10:
            return None
        if hasattr(df.columns, "get_level_values") and df.columns.nlevels > 1:
            df.columns = df.columns.get_level_values(0)
        vol = df["Volume"].iloc[-50:] if len(df) >= 50 else df["Volume"]
        return float(vol.mean())
    except Exception as e:
        logger.warning("Failed to fetch adv50 for %s: %s", ticker, e)
        return None


def check_rvol_and_cancel(dry_run: bool = False) -> None:
    """10:30 AM check: cancel PLACED buy-stop orders if intraday RVOL < 1.5x expected.

    For each PLACED order:
      expected_vol = adv50 * (_RVOL_CHECK_ELAPSED / _SESSION_MINUTES)
      If actual_vol < expected_vol * _RVOL_THRESHOLD → cancel + log 'Low RVOL'
      If actual_vol >= threshold → log confirmation, leave order live
    """
    placed = db.get_placed_trades()
    if not placed:
        logger.info("RVOL check: no PLACED orders to check")
        return

    logger.info("RVOL check (10:30 AM): checking %d placed order(s)", len(placed))

    for trade in placed:
        trade_id = trade["id"]
        ticker = trade["ticker"]
        order_id = trade["alpaca_order_id"]

        if not order_id or order_id == "DRY_RUN":
            logger.info("RVOL check: %s — skipping (no live order_id)", ticker)
            continue

        # Get 50-day ADV to compute expected volume at 10:30 AM
        adv50 = _fetch_adv50_yfinance(ticker)
        if adv50 is None or adv50 <= 0:
            logger.warning("RVOL check: %s — could not fetch adv50, skipping", ticker)
            continue

        expected_vol = adv50 * (_RVOL_CHECK_ELAPSED / _SESSION_MINUTES)
        required_vol = expected_vol * _RVOL_THRESHOLD

        actual_vol = _fetch_intraday_volume(ticker)
        if actual_vol is None:
            logger.warning("RVOL check: %s — could not fetch intraday volume, skipping", ticker)
            continue

        rvol = actual_vol / expected_vol if expected_vol > 0 else 0.0

        if actual_vol < required_vol:
            logger.info(
                "RVOL check: %s — RVOL=%.2fx (actual=%.0f expected=%.0f required=%.0f) "
                "— Low RVOL — order cancelled",
                ticker, rvol, actual_vol, expected_vol, required_vol,
            )
            if not dry_run:
                cancel_order(order_id)
                db.update_trade_status(
                    trade_id, "RVOL_CANCELLED",
                    exit_date=datetime.now(ET).isoformat(),
                    exit_reason="Low RVOL — order cancelled",
                )
            else:
                logger.info("[DRY RUN] Would cancel %s due to low RVOL", ticker)
        else:
            logger.info(
                "RVOL check: %s — RVOL=%.2fx (actual=%.0f expected=%.0f) — confirmed, order stays live",
                ticker, rvol, actual_vol, expected_vol,
            )
