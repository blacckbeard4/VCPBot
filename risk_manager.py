"""VCPBot Phase 5 — Risk calculation and position sizing (pure math, no AI).

Rules:
  - risk_per_trade = account_equity * risk_pct
    - Normal mode: risk_pct = 2% (RISK_PCT_NORMAL)
    - FTD mode:    risk_pct = 1% (RISK_PCT_FTD)
  - risk_unit = pivot_price - stop_loss_price
  - position_size = risk_per_trade / risk_unit  (fractional shares supported)
  - Stop distance must be <= 7% of pivot (checked already in vcp_detector)
  - Max 5 open positions at once
  - Total combined risk of all open positions must not exceed 10% of equity
  - If portfolio heat limit hit → queue but do not execute
"""

import logging
import math
from typing import Optional

from config import (
    ACCOUNT_VALUE,
    RISK_PCT_NORMAL, RISK_PCT_FTD,
    MAX_OPEN_POSITIONS, MAX_TOTAL_PORTFOLIO_RISK_PCT,
    MAX_STOP_PCT, TARGET_PCT, BUY_STOP_OFFSET,
)
import db

logger = logging.getLogger(__name__)


def compute_position_sizes(
    vcp_setups: list[dict],
    open_positions: list,
    account_value: float,
    ftd_mode: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """Compute position sizes for all VCP setups and apply risk rules.

    Args:
        vcp_setups: List of VCP setup dicts from vcp_detector (sorted by RS rank desc).
        open_positions: Open trade rows from DB.
        account_value: Current account equity.
        ftd_mode: If True, use 1% risk per trade instead of 2%.
        dry_run: If True, compute sizes but don't trigger any side effects.

    Returns list of decision dicts:
        {ticker, decision, shares, entry_price, stop_price, target_price,
         risk_pct, risk_dollars, reason}
    """
    if not vcp_setups:
        return []

    risk_pct = RISK_PCT_FTD if ftd_mode else RISK_PCT_NORMAL
    risk_per_trade = account_value * risk_pct

    # Count open positions
    n_open = len(open_positions)

    # Compute current portfolio heat = sum of all open position risk as % of equity
    portfolio_heat = _compute_portfolio_heat(open_positions, account_value)

    decisions: list[dict] = []
    positions_used = n_open

    for setup in vcp_setups:
        ticker = setup["ticker"]
        pivot = setup["pivot_price"]
        stop = setup["stop_loss_price"]

        entry_price = round(pivot + BUY_STOP_OFFSET, 2)
        target_price = round(entry_price * (1 + TARGET_PCT), 2)
        risk_unit = entry_price - stop

        # Validate risk unit
        if risk_unit <= 0:
            decisions.append(_reject(ticker, "Risk unit <= 0 (stop above entry)"))
            continue

        # Hard cap: stop distance > 7% of pivot (matches vcp_detector check)
        stop_pct = risk_unit / pivot
        if stop_pct > MAX_STOP_PCT:
            decisions.append(_reject(ticker, f"Stop {stop_pct:.1%} exceeds max {MAX_STOP_PCT:.0%} of pivot"))
            continue

        # Max positions
        if positions_used >= MAX_OPEN_POSITIONS:
            decisions.append(_reject(
                ticker,
                f"Max {MAX_OPEN_POSITIONS} positions reached — queued for next opening",
            ))
            continue

        # Portfolio heat cap
        this_trade_risk_pct = risk_per_trade / account_value
        if portfolio_heat + this_trade_risk_pct > MAX_TOTAL_PORTFOLIO_RISK_PCT:
            decisions.append(_reject(
                ticker,
                f"Portfolio heat {portfolio_heat:.1%} + {this_trade_risk_pct:.1%} "
                f"would exceed {MAX_TOTAL_PORTFOLIO_RISK_PCT:.0%} cap",
            ))
            continue

        # Position size = risk_per_trade / risk_unit (fractional shares)
        raw_shares = risk_per_trade / risk_unit
        # Round to 2 decimal places for fractional share support
        shares = round(raw_shares, 2)

        if shares < 0.01:
            decisions.append(_reject(ticker, "Position size < 0.01 shares after risk calc"))
            continue

        risk_dollars = round(shares * risk_unit, 2)

        logger.info(
            "APPROVE %s: %.2f shares @ entry=%.2f stop=%.2f target=%.2f "
            "risk=$%.2f (%.1f%% of account)",
            ticker, shares, entry_price, stop, target_price,
            risk_dollars, risk_pct * 100,
        )

        decisions.append({
            "ticker": ticker,
            "decision": "APPROVE",
            "shares": shares,
            "entry_price": entry_price,
            "stop_price": stop,
            "target_price": target_price,
            "pivot_price": pivot,
            "risk_pct": risk_pct,
            "risk_dollars": risk_dollars,
            "rs_rank": setup.get("rs_rank", 0),
            "base_duration_weeks": setup.get("base_duration_weeks", 0),
            "final_contraction_depth": setup.get("final_contraction_depth", 0),
            "contraction_depths": setup.get("contraction_depths", []),
            "reason": f"RS_Rank={setup.get('rs_rank', 0):.0f}, "
                      f"base={setup.get('base_duration_weeks', 0)}w, "
                      f"contractions={[f'{d:.1%}' for d in setup.get('contraction_depths', [])]}",
        })

        portfolio_heat += this_trade_risk_pct
        positions_used += 1

    approved = sum(1 for d in decisions if d["decision"] == "APPROVE")
    rejected = len(decisions) - approved
    logger.info("Risk manager: %d approved, %d rejected (heat=%.1f%%)",
                approved, rejected, portfolio_heat * 100)

    return decisions


def _reject(ticker: str, reason: str) -> dict:
    """Build a REJECT decision dict."""
    logger.info("REJECT %s: %s", ticker, reason)
    return {
        "ticker": ticker,
        "decision": "REJECT",
        "shares": 0.0,
        "entry_price": 0.0,
        "stop_price": 0.0,
        "target_price": 0.0,
        "risk_pct": 0.0,
        "risk_dollars": 0.0,
        "reason": reason,
    }


def _compute_portfolio_heat(open_positions: list, account_value: float) -> float:
    """Compute combined open risk as fraction of account equity.

    For each open position: risk = (entry_price - stop_price) * shares / account_value
    """
    if not open_positions or account_value <= 0:
        return 0.0

    total_risk = 0.0
    for pos in open_positions:
        try:
            entry = float(pos["entry_price"] or 0)
            stop = float(pos["stop_price"] or 0)
            shares = float(pos["shares"] or 0)
            if entry > 0 and stop > 0 and shares > 0:
                risk = max(0.0, (entry - stop) * shares)
                total_risk += risk
        except (TypeError, ValueError):
            continue

    return total_risk / account_value
