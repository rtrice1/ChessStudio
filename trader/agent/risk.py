"""Hard risk limits. Every order the strategist wants to place goes through
check_order() first. These limits are enforced in code, not in prompts, so no
model — however clever or however confused — can exceed them. Changing this
file is a human decision.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    # Max fraction of account equity in any single symbol (post-trade).
    max_position_pct: float = 0.10
    # Max fraction of equity deployed across all positions (no margin).
    max_gross_exposure_pct: float = 1.00
    # Single-order notional cap as a fraction of equity (fat-finger guard).
    max_order_pct: float = 0.15
    # Halt all new buys if equity drops this fraction below the day's start.
    daily_loss_halt_pct: float = 0.02
    # Absolute floor: never let cash go below this on a buy.
    min_cash_reserve: float = 0.0


@dataclass
class RiskVerdict:
    approved: bool
    reason: str

    def __bool__(self) -> bool:
        return self.approved


KILL_SWITCH_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "HALT")


def kill_switch_engaged(path: str = KILL_SWITCH_FILE) -> bool:
    """A human (or any agent) can halt all trading by creating this file.
    Deleting it is a human decision."""
    return os.path.exists(path)


def check_order(
    account: dict,
    symbol: str,
    instruction: str,
    quantity: int,
    est_price: float,
    day_open_equity: float | None = None,
    limits: RiskLimits | None = None,
) -> RiskVerdict:
    """Validate a proposed order against hard limits.

    account: broker account snapshot dict (cash, equity, positions).
    est_price: current ask (buys) or bid (sells) used to estimate notional.
    day_open_equity: equity at session start, for the daily-loss circuit breaker.
    """
    limits = limits or RiskLimits()

    if kill_switch_engaged():
        return RiskVerdict(False, "kill switch engaged (data/HALT exists)")
    if quantity <= 0:
        return RiskVerdict(False, f"non-positive quantity {quantity}")
    if est_price <= 0:
        return RiskVerdict(False, f"non-positive estimated price {est_price}")

    equity = float(account.get("equity", 0.0))
    cash = float(account.get("cash", 0.0))
    positions = {p["symbol"]: p for p in account.get("positions", [])}
    notional = quantity * est_price

    if equity <= 0:
        return RiskVerdict(False, "account equity is non-positive")

    if instruction == "SELL":
        held = int(positions.get(symbol, {}).get("quantity", 0))
        if quantity > held:
            return RiskVerdict(False, f"would short: selling {quantity} of {held} held {symbol}")
        return RiskVerdict(True, "sell within held quantity")

    if instruction != "BUY":
        return RiskVerdict(False, f"unknown instruction {instruction!r}")

    # --- BUY checks ---
    if day_open_equity is not None and day_open_equity > 0:
        drawdown = (day_open_equity - equity) / day_open_equity
        if drawdown >= limits.daily_loss_halt_pct:
            return RiskVerdict(
                False,
                f"daily loss circuit breaker: down {drawdown:.2%} "
                f">= {limits.daily_loss_halt_pct:.2%} from day open",
            )

    if notional > limits.max_order_pct * equity:
        return RiskVerdict(
            False,
            f"order notional {notional:.2f} exceeds "
            f"{limits.max_order_pct:.0%} of equity ({equity:.2f})",
        )

    if notional > cash - limits.min_cash_reserve:
        return RiskVerdict(False, f"insufficient cash: need {notional:.2f}, have {cash:.2f}")

    current_pos_value = float(positions.get(symbol, {}).get("marketValue", 0.0))
    if current_pos_value + notional > limits.max_position_pct * equity:
        return RiskVerdict(
            False,
            f"position in {symbol} would be {(current_pos_value + notional) / equity:.2%} "
            f"of equity, cap is {limits.max_position_pct:.0%}",
        )

    gross = sum(float(p.get("marketValue", 0.0)) for p in account.get("positions", []))
    if gross + notional > limits.max_gross_exposure_pct * equity:
        return RiskVerdict(
            False,
            f"gross exposure would be {(gross + notional) / equity:.2%}, "
            f"cap is {limits.max_gross_exposure_pct:.0%}",
        )

    return RiskVerdict(True, "within all limits")
