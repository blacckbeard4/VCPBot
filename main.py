"""VCPBot — APScheduler entry point and pipeline orchestration.

Schedule (all US/Eastern):
  4:05 PM Mon-Fri  → run_scan_pipeline()    (Phases 1-5: regime→scan→VCP→risk)
  9:30 AM Mon-Fri  → run_execution_job()    (Phase 6: place buy stop orders)
  30 min intervals → run_intraday_monitor() (Phase 7: intraday checks)
  4:05 PM Mon-Fri  → run_eod_monitor_job()  (Phase 7: EOD + fill checks)
  Sunday 8:00 AM   → run_weekly_report_job()

Usage:
  python main.py             # run scheduler (production)
  python main.py --dry-run   # full pipeline without placing any orders
"""

import argparse
import gc
import logging
import signal
import sys
import traceback
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
import pandas_market_calendars as mcal

from config import TIMEZONE, ALPACA_PAPER, ACCOUNT_VALUE
import db
import regime as regime_mod
from tickers import get_full_universe
import scanner
import vcp_detector
import htf_detector
import risk_manager
import executor
import monitor
from notifier import (
    send_alert, send_error_alert, send_pipeline_summary,
    send_weekly_report, send_cash_mode_alert, send_cash_mode_exit_alert,
    send_ftd_alert, send_vcp_signal_alert, send_htf_signal_alert,
)

logger = logging.getLogger(__name__)
ET = ZoneInfo(TIMEZONE)
NYSE = mcal.get_calendar("NYSE")

# Set by CLI arg or always False
DRY_RUN: bool = False


# ═══════════════════════════════════════════════════════════
# MARKET CALENDAR
# ═══════════════════════════════════════════════════════════


def is_market_day(dt: datetime | None = None) -> bool:
    """Check if given date is a NYSE trading day."""
    if dt is None:
        dt = datetime.now(ET)
    d = dt.date() if hasattr(dt, "date") else dt
    return len(NYSE.valid_days(start_date=d, end_date=d)) > 0


# ═══════════════════════════════════════════════════════════
# CONFIG VALIDATION
# ═══════════════════════════════════════════════════════════


def validate_config() -> bool:
    """Check required env vars. Returns True if valid."""
    from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    missing = []
    if not ALPACA_API_KEY:
        missing.append("ALPACA_API_KEY")
    if not ALPACA_SECRET_KEY:
        missing.append("ALPACA_SECRET_KEY")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        logger.error("Missing required env vars: %s", ", ".join(missing))
        return False
    return True


# ═══════════════════════════════════════════════════════════
# SCHEDULED JOBS
# ═══════════════════════════════════════════════════════════


def run_scan_pipeline() -> None:
    """4:05 PM Mon-Fri — Phases 1-5: regime detection → scan → VCP → risk sizing."""
    if not is_market_day():
        logger.info("Not a market day — skipping pipeline")
        return

    logger.info("=" * 60)
    logger.info("VCP SCAN PIPELINE START%s", " [DRY RUN]" if DRY_RUN else "")
    logger.info("=" * 60)

    today = datetime.now(ET).strftime("%Y-%m-%d")

    # ── Phase 1: Regime detection ──
    try:
        regime = regime_mod.detect_regime()
    except Exception as e:
        msg = f"Regime detection failed: {e}"
        logger.error(msg)
        db.log_error("regime", "RuntimeError", str(e), traceback_str=traceback.format_exc())
        send_error_alert("regime", msg)
        return

    # Notify mode changes
    prior_state = db.get_regime_state()
    if regime["cash_mode"] and (prior_state is None or not prior_state["cash_mode"]):
        send_cash_mode_alert(regime["spy_close"], regime["spy_sma200"])
    elif not regime["cash_mode"] and prior_state and prior_state["cash_mode"]:
        send_cash_mode_exit_alert(regime["spy_close"], regime["spy_sma200"])
    if regime["ftd_mode"] and (prior_state is None or not prior_state["ftd_mode"]):
        send_ftd_alert("SPY", regime.get("ftd_gain_pct", 0.0), regime.get("rally_day_count", 0))

    # CASH MODE: no new entries, nothing more to do
    if regime["cash_mode"]:
        logger.info("CASH MODE active — skipping scan. Watching for FTD / SMA200 reclaim.")
        db.insert_scan_log(
            run_date=today,
            regime="CASH",
            tickers_scanned=0,
            tickers_phase2=0,
            tickers_phase3=0,
            vcp_setups=0,
            orders_queued=0,
        )
        return

    # ── Phase 2+3: Universe screen + Trend Template + RS Rank ──
    all_rejections: list[dict] = []

    try:
        universe = get_full_universe()
        if not universe:
            logger.error("Empty ticker universe — check tickers.py")
            return
        logger.info("Universe: %d tickers", len(universe))

        scan_results, phase3_rejections = scanner.scan_universe(universe)
        all_rejections.extend(phase3_rejections)
        logger.info("Phase 2+3: %d tickers passed", len(scan_results))
    except Exception as e:
        msg = f"Scanner failed: {e}"
        logger.error(msg)
        db.log_error("scanner", str(type(e).__name__), str(e), traceback_str=traceback.format_exc())
        send_error_alert("scanner", msg)
        return

    gc.collect()

    if not scan_results:
        db.bulk_insert_rejections(today, all_rejections)
        db.insert_scan_log(
            run_date=today, regime=regime["regime_label"],
            tickers_scanned=len(universe), tickers_phase2=0,
            tickers_phase3=0, vcp_setups=0, orders_queued=0,
        )
        return

    phase3_count = len(scan_results)

    # ── Phase 4a: VCP pattern detection ──
    try:
        vcp_setups, vcp_rejections = vcp_detector.detect_vcp_batch(scan_results)
        # Separate the HTF candidate sentinel from normal rejections
        htf_candidate_tickers: list[str] = []
        for r in vcp_rejections:
            if r["ticker"] == "__HTF_CANDIDATES__":
                htf_candidate_tickers = r["reason"].split(",") if r["reason"] else []
            else:
                all_rejections.append(r)
        logger.info("Phase 4a (VCP): %d setups, %d HTF candidates",
                    len(vcp_setups), len(htf_candidate_tickers))
    except Exception as e:
        logger.error("VCP detection failed: %s", e)
        db.log_error("vcp_detector", str(type(e).__name__), str(e), traceback_str=traceback.format_exc())
        vcp_setups = []
        htf_candidate_tickers = []

    gc.collect()

    # ── Phase 4b: HTF pattern detection ──
    try:
        htf_setups, htf_rejections = htf_detector.detect_htf_batch(
            scan_results, htf_candidate_tickers
        )
        all_rejections.extend(htf_rejections)
        logger.info("Phase 4b (HTF): %d setups found", len(htf_setups))
    except Exception as e:
        logger.error("HTF detection failed: %s", e)
        db.log_error("htf_detector", str(type(e).__name__), str(e), traceback_str=traceback.format_exc())
        htf_setups = []

    gc.collect()

    all_setups = vcp_setups + htf_setups

    if not all_setups:
        db.bulk_insert_rejections(today, all_rejections)
        db.insert_scan_log(
            run_date=today, regime=regime["regime_label"],
            tickers_scanned=len(universe), tickers_phase2=phase3_count,
            tickers_phase3=phase3_count, vcp_setups=0, orders_queued=0,
        )
        return

    # ── Phase 5: Risk calculation + position sizing ──
    open_positions = db.get_open_trades()
    live_account_value = executor.get_portfolio_value() or ACCOUNT_VALUE
    ftd_mode = regime["ftd_mode"]

    try:
        decisions = risk_manager.compute_position_sizes(
            vcp_setups=all_setups,
            open_positions=open_positions,
            account_value=live_account_value,
            ftd_mode=ftd_mode,
            dry_run=DRY_RUN,
        )
    except Exception as e:
        logger.error("Risk manager failed: %s", e)
        db.log_error("risk_manager", str(type(e).__name__), str(e), traceback_str=traceback.format_exc())
        decisions = []

    # Collect risk rejections
    for decision in decisions:
        if decision["decision"] != "APPROVE":
            all_rejections.append({
                "ticker": decision["ticker"],
                "phase": "RISK",
                "reason": decision.get("reason", "risk check failed"),
            })

    # ── Write approved trades as PENDING to DB ──
    orders_queued = 0
    for decision in decisions:
        if decision["decision"] != "APPROVE":
            continue
        ticker = decision["ticker"]

        # Find matching setup (VCP first, then HTF)
        setup = next((s for s in all_setups if s["ticker"] == ticker), None)
        if setup is None:
            continue

        pattern = setup.get("pattern_type", "VCP")

        trade_id = db.insert_trade(
            ticker=ticker,
            stop_price=decision["stop_price"],
            target_1_price=decision["target_price"],
            shares=decision["shares"],
            entry_date=today,
            pivot_price=decision["pivot_price"],
            rs_rank=decision["rs_rank"],
            base_duration_weeks=decision["base_duration_weeks"],
            contraction_depth_pct=decision["final_contraction_depth"],
            regime_at_entry=regime["regime_label"],
            analyst_rationale=decision["reason"],
            status="PENDING" if not DRY_RUN else "DRY_RUN",
            pattern_type=pattern,
        )

        if pattern == "HTF":
            send_htf_signal_alert(
                ticker=ticker,
                pivot=decision["pivot_price"],
                stop=decision["stop_price"],
                rs_rank=decision["rs_rank"],
                gain_8w_pct=setup.get("gain_8w_pct", 0.0),
                consolidation_depth_pct=setup.get("consolidation_depth_pct", 0.0),
                days_consolidating=setup.get("days_consolidating", 0),
            )
        else:
            send_vcp_signal_alert(
                ticker=ticker,
                pivot=decision["pivot_price"],
                stop=decision["stop_price"],
                rs_rank=decision["rs_rank"],
                base_weeks=decision["base_duration_weeks"],
                contractions=decision["contraction_depths"],
            )

        # Log to CSV at signal time (entry price not known yet)
        db.log_trade_to_csv(
            ticker=ticker,
            entry_price=decision["entry_price"],
            stop_price=decision["stop_price"],
            target_price=decision["target_price"],
            shares=decision["shares"],
            account_equity=live_account_value,
            rs_rank=decision["rs_rank"],
            base_weeks=decision["base_duration_weeks"],
            contraction_depth_pct=decision["final_contraction_depth"],
        )

        orders_queued += 1

    db.bulk_insert_rejections(today, all_rejections)
    db.insert_scan_log(
        run_date=today,
        regime=regime["regime_label"],
        tickers_scanned=len(universe),
        tickers_phase2=phase3_count,
        tickers_phase3=phase3_count,
        vcp_setups=len(vcp_setups) + len(htf_setups),
        orders_queued=orders_queued,
    )

    send_pipeline_summary(
        cash_mode=regime["cash_mode"],
        ftd_mode=ftd_mode,
        tickers_scanned=len(universe),
        tickers_phase2=phase3_count,
        tickers_phase3=phase3_count,
        vcp_setups=len(vcp_setups) + len(htf_setups),
        orders_queued=orders_queued,
    )

    logger.info(
        "PIPELINE COMPLETE: universe=%d, phase3=%d, vcp=%d, htf=%d, queued=%d%s",
        len(universe), phase3_count, len(vcp_setups), len(htf_setups), orders_queued,
        " [DRY RUN — no orders placed]" if DRY_RUN else "",
    )


def run_execution_job() -> None:
    """9:30 AM Mon-Fri — Phase 6: place buy stop-limit orders."""
    if not is_market_day():
        return
    if DRY_RUN:
        logger.info("[DRY RUN] Skipping order placement")
        return
    try:
        executor.run_execution(dry_run=DRY_RUN)
    except Exception as e:
        logger.error("Execution job failed: %s", e)
        db.log_error("executor", str(type(e).__name__), str(e), traceback_str=traceback.format_exc())
        send_error_alert("executor", str(e))


def run_rvol_check_job() -> None:
    """10:30 AM Mon-Fri — RVOL confirmation: cancel orders with insufficient intraday volume."""
    if not is_market_day():
        return
    try:
        executor.check_rvol_and_cancel(dry_run=DRY_RUN)
    except Exception as e:
        logger.error("RVOL check job failed: %s", e)
        db.log_error("executor_rvol", str(type(e).__name__), str(e), traceback_str=traceback.format_exc())
        send_error_alert("rvol_check", str(e))


def run_intraday_monitor_job() -> None:
    """Every 30 min 9:30am-4pm — Phase 7: intraday position checks."""
    if not is_market_day():
        return
    now = datetime.now(ET)
    if now.hour < 9 or (now.hour == 9 and now.minute < 30) or now.hour >= 16:
        return
    try:
        monitor.run_intraday_monitor()
    except Exception as e:
        logger.error("Intraday monitor failed: %s", e)
        db.log_error("monitor_intraday", str(type(e).__name__), str(e))


def run_eod_monitor_job() -> None:
    """4:05 PM Mon-Fri — EOD: fill checks, stale cancels, portfolio snapshot."""
    if not is_market_day():
        return
    try:
        monitor.run_eod_monitor()
    except Exception as e:
        logger.error("EOD monitor failed: %s", e)
        db.log_error("monitor_eod", str(type(e).__name__), str(e), traceback_str=traceback.format_exc())
        send_error_alert("eod_monitor", str(e))


def run_weekly_report_job() -> None:
    """Sunday 8am — compute and send weekly performance stats."""
    try:
        from datetime import timedelta
        now = datetime.now(ET)
        week_end = now.strftime("%Y-%m-%d")
        week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
        trades = db.get_trades_by_date_range(week_start, week_end)
        closed = [t for t in trades if t["status"] in ("STOPPED", "TARGET_HIT")]

        stats = db.compute_stats_from_csv()

        if not closed and not stats:
            send_weekly_report("No trades closed this week.")
            return

        wins = [t for t in closed if (t["pnl"] or 0) > 0]
        losses = [t for t in closed if (t["pnl"] or 0) <= 0]
        total_pnl = sum(float(t["pnl"] or 0) for t in closed)
        win_rate = len(wins) / len(closed) * 100 if closed else 0

        report_lines = [
            f"Week: {week_start} to {week_end}",
            f"Trades closed: {len(closed)} ({len(wins)}W/{len(losses)}L)",
            f"Win rate: {win_rate:.0f}%",
            f"Weekly P&L: ${total_pnl:+,.2f}",
        ]
        if stats:
            report_lines += [
                "",
                "── All-Time Stats ──",
                f"Total trades: {stats.get('total_trades', 0)}",
                f"Win rate: {stats.get('win_rate', 0):.0%}",
                f"Avg win: ${stats.get('avg_win', 0):+.2f} | Avg loss: ${stats.get('avg_loss', 0):+.2f}",
                f"Expectancy: ${stats.get('expectancy', 0):+.2f} per trade",
                f"Total P&L: ${stats.get('total_pnl', 0):+,.2f}",
            ]

        send_weekly_report("\n".join(report_lines))
        logger.info("Weekly report sent: %d trades, $%.2f P&L", len(closed), total_pnl)

    except Exception as e:
        logger.error("Weekly report failed: %s", e)
        send_error_alert("weekly_report", str(e))


# ═══════════════════════════════════════════════════════════
# LOGGING & ENTRY POINT
# ═══════════════════════════════════════════════════════════


def setup_logging() -> None:
    """Configure logging to stdout + rotating file."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    file_handler = RotatingFileHandler(
        "vcpbot.log", maxBytes=5 * 1024 * 1024, backupCount=3,
    )
    file_handler.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(file_handler)
    for noisy in ("yfinance", "urllib3", "apscheduler", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    """Entry point — set up logging, DB, scheduler, and run."""
    global DRY_RUN

    parser = argparse.ArgumentParser(description="VCPBot — VCP Momentum Breakout trading bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run full pipeline and log signals without placing any Alpaca orders",
    )
    parser.add_argument(
        "--run-now",
        action="store_true",
        help="Run the scan pipeline immediately (for testing), then exit",
    )
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    setup_logging()

    if not validate_config():
        logger.error("Config validation failed — exiting")
        sys.exit(1)

    db.init_db()

    mode_str = "PAPER" if ALPACA_PAPER else "\u26a0\ufe0f LIVE \u26a0\ufe0f"
    dry_str = " [DRY RUN]" if DRY_RUN else ""
    logger.info("VCPBot started | Mode: %s%s | Account: $%.0f", mode_str, dry_str, ACCOUNT_VALUE)

    # Immediate one-shot run for testing
    if args.run_now:
        logger.info("--run-now: executing scan pipeline immediately")
        run_scan_pipeline()
        return

    # Set up scheduler
    sched = BlockingScheduler(timezone=ET)

    # 4:05 PM Mon-Fri: full scan pipeline (Phases 1-5)
    sched.add_job(
        run_scan_pipeline,
        CronTrigger(hour=16, minute=5, day_of_week="mon-fri", timezone=ET),
        id="scan_pipeline", name="VCP Scan Pipeline",
        misfire_grace_time=600,
    )

    # 9:30 AM Mon-Fri: order execution (Phase 6)
    sched.add_job(
        run_execution_job,
        CronTrigger(hour=9, minute=30, day_of_week="mon-fri", timezone=ET),
        id="execution", name="Order Execution",
        misfire_grace_time=120,
    )

    # 10:30 AM Mon-Fri: RVOL confirmation — cancel low-volume orders
    sched.add_job(
        run_rvol_check_job,
        CronTrigger(hour=10, minute=30, day_of_week="mon-fri", timezone=ET),
        id="rvol_check", name="RVOL Confirmation Check",
        misfire_grace_time=120,
    )

    # Every 30 min 9:30am-4pm: intraday monitor (Phase 7)
    sched.add_job(
        run_intraday_monitor_job,
        CronTrigger(hour="9-15", minute="0,30", day_of_week="mon-fri", timezone=ET),
        id="intraday_monitor", name="Intraday Monitor",
        misfire_grace_time=60,
    )

    # 4:05 PM Mon-Fri: EOD monitor + fill checks
    sched.add_job(
        run_eod_monitor_job,
        CronTrigger(hour=16, minute=5, day_of_week="mon-fri", timezone=ET),
        id="eod_monitor", name="EOD Monitor",
        misfire_grace_time=300,
    )

    # Sunday 8am: weekly report
    sched.add_job(
        run_weekly_report_job,
        CronTrigger(day_of_week="sun", hour=8, minute=0, timezone=ET),
        id="weekly_report", name="Weekly Report",
        misfire_grace_time=3600,
    )

    def shutdown(signum, frame):
        logger.info("Received signal %s — shutting down", signum)
        sched.shutdown(wait=False)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("Scheduler starting with %d jobs", len(sched.get_jobs()))
    for job in sched.get_jobs():
        logger.info("  Job: %-22s %s", job.name, job.trigger)

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
