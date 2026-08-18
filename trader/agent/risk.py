"""Hard risk limits. Every order the strategist wants to place goes through
check_order() first. These limits are enforced in code, not in prompts, so no
model — however clever or however confused — can exceed them. Changing this
file is a human decision.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# OCC-style option symbol: ROOT + YYMMDD + C/P + strike*1000 (8 digits).
_OCC_RE = re.compile(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$")


def is_option(symbol: str) -> bool:
    return bool(_OCC_RE.match(symbol or ""))


def underlying(symbol: str) -> str:
    """The stock an exposure is really in: OCC option -> its root."""
    m = _OCC_RE.match(symbol or "")
    return m.group(1) if m else symbol


# Correlated clusters (human-approved addition, 2026-08-17): most of the
# watchlist is US megacap/index beta that moves together intraday —
# holding several of these long at once is ONE bet taken several times,
# and "0.5% risk per trade" quietly multiplies. Entries are capped at
# max_correlated_positions distinct names per group (adds to a name
# already held are unaffected, sells never blocked). Like every limit in
# this file, the groups change only by a human editing them.
CORRELATION_GROUPS: dict[str, frozenset] = {
    "us_equity_beta": frozenset({"SPY", "QQQ", "AAPL", "MSFT", "NVDA",
                                 "GOOGL", "AMZN", "TSLA"}),
}


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
    # Day-trading: hard cap on round-trip-generating orders per day.
    max_daily_trades: int = 40
    # Day-trading: no new entries after this fraction of the session has
    # elapsed (0.9 of a 6.5h session = ~15:05 ET). Exits are always allowed.
    entry_cutoff_session_pct: float = 0.9
    # Options: LONG contracts only, and every dollar of premium is treated
    # as if it goes to zero (for a long option, it can). Per-order and
    # total premium-at-risk caps as fractions of equity.
    max_option_premium_pct: float = 0.02
    max_total_option_premium_pct: float = 0.06
    # FINRA pattern-day-trader rule: below this equity in a MARGIN account,
    # more than 3 day trades in 5 rolling sessions flags the account and
    # freezes it. Enforced here so a small account physically can't trip
    # it. A cash account has no PDT rule — a human who has confirmed the
    # account type is cash may raise max_day_trades_5d (human edit only,
    # like every limit in this file).
    pdt_min_equity: float = 25_000.0
    max_day_trades_5d: int = 3
    # Max distinct names held per correlation group (see CORRELATION_GROUPS).
    max_correlated_positions: int = 2


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
    trades_today: int | None = None,
    session_pct: float | None = None,
    day_trades_5d: int | None = None,
    pdt_equity: float | None = None,
) -> RiskVerdict:
    """Validate a proposed order against hard limits.

    account: broker account snapshot dict (cash, equity, positions).
    est_price: current ask (buys) or bid (sells) used to estimate notional.
    day_open_equity: equity at session start, for the daily-loss circuit breaker.
    trades_today: filled order count so far today, for the trade-count cap.
    session_pct: fraction of the trading session elapsed (0.0 open, 1.0 close);
                 new entries are refused late in the day so the mandatory
                 end-of-day flatten never has fresh positions to unwind.
    day_trades_5d: round trips over the rolling 5 sessions (this one
                 included), for the sub-$25k PDT guard. None = not tracked
                 (sims); the live runner always passes it.
    pdt_equity: the equity FINRA's PDT rule actually looks at — the whole
                 brokerage account, not this desk's allocation. When the
                 desk trades a $10k slice of a $25k+ account, the runner
                 passes the real account equity here so the guard doesn't
                 bind on the slice. None falls back to book equity, which
                 is the conservative direction.
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
    # PDT guard: entries stop before the day-trade budget can be exceeded,
    # because every entry on this desk becomes a day trade (flat by close).
    # SELLs are never blocked — closing is always allowed and always safe.
    # The rule keys off the ACCOUNT's equity (pdt_equity), not the desk's
    # allocation; without it we assume the book is the account.
    pdt_eq = pdt_equity if pdt_equity is not None else equity
    if (day_trades_5d is not None and pdt_eq < limits.pdt_min_equity
            and day_trades_5d >= limits.max_day_trades_5d):
        return RiskVerdict(
            False,
            f"PDT guard: {day_trades_5d} day trades in 5 sessions with "
            f"account equity {pdt_eq:.0f} < {limits.pdt_min_equity:.0f} "
            f"(cash account? raise max_day_trades_5d by hand)",
        )

    if trades_today is not None and trades_today >= limits.max_daily_trades:
        return RiskVerdict(
            False,
            f"daily trade cap: {trades_today} >= {limits.max_daily_trades}",
        )

    if session_pct is not None and session_pct >= limits.entry_cutoff_session_pct:
        return RiskVerdict(
            False,
            f"entry cutoff: {session_pct:.0%} of session elapsed "
            f">= {limits.entry_cutoff_session_pct:.0%}; exits only",
        )

    if day_open_equity is not None and day_open_equity > 0:
        drawdown = (day_open_equity - equity) / day_open_equity
        if drawdown >= limits.daily_loss_halt_pct:
            return RiskVerdict(
                False,
                f"daily loss circuit breaker: down {drawdown:.2%} "
                f">= {limits.daily_loss_halt_pct:.2%} from day open",
            )

    # Correlation cap: applies to shares AND options (an NVDA call is NVDA
    # exposure). Counts DISTINCT other underlyings held in the same group,
    # so adding to an existing position is never blocked by this.
    und = underlying(symbol)
    for group_name, members in CORRELATION_GROUPS.items():
        if und not in members:
            continue
        held_in_group = {underlying(p.get("symbol", ""))
                         for p in account.get("positions", [])
                         if float(p.get("quantity", 0)) > 0
                         and underlying(p.get("symbol", "")) in members}
        held_in_group.discard(und)
        if len(held_in_group) >= limits.max_correlated_positions:
            return RiskVerdict(
                False,
                f"correlation cap: {symbol} joins group '{group_name}' "
                f"already holding {sorted(held_in_group)} — "
                f"max {limits.max_correlated_positions} correlated names, "
                f"this would be one bet taken "
                f"{len(held_in_group) + 1} times",
            )

    if notional > limits.max_order_pct * equity:
        return RiskVerdict(
            False,
            f"order notional {notional:.2f} exceeds "
            f"{limits.max_order_pct:.0%} of equity ({equity:.2f})",
        )

    if notional > cash - limits.min_cash_reserve:
        return RiskVerdict(False, f"insufficient cash: need {notional:.2f}, have {cash:.2f}")

    if is_option(symbol):
        # Long-options-only is structural (the engine cannot short), so the
        # BUY checks are about premium at risk: this order's premium, plus
        # premium already deployed across all option positions, both capped.
        if notional > limits.max_option_premium_pct * equity:
            return RiskVerdict(
                False,
                f"option premium {notional:.2f} exceeds "
                f"{limits.max_option_premium_pct:.0%} of equity per order",
            )
        deployed = sum(float(p.get("marketValue", 0.0))
                       for p in account.get("positions", [])
                       if is_option(p.get("symbol", "")))
        if deployed + notional > limits.max_total_option_premium_pct * equity:
            return RiskVerdict(
                False,
                f"total option premium would be "
                f"{(deployed + notional) / equity:.2%} of equity, cap is "
                f"{limits.max_total_option_premium_pct:.0%}",
            )
        if notional > cash - limits.min_cash_reserve:
            return RiskVerdict(False,
                               f"insufficient cash for premium {notional:.2f}")
        return RiskVerdict(True, "option premium within caps")

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
