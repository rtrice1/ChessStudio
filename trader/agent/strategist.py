"""Tier-3 decision engine — day-trading edition.

Architecture: **the LLM plans the day; code trades the plan.** An LLM should
not be making 1-minute-bar decisions (too slow, too expensive, too tempted to
narrate), so the division of labor is:

- At the open, Tier 3 (Claude) reads the desk context and the first snapshots
  and emits a `DayPlan`: which symbols are in play today, directional bias,
  the day's risk budget, and a max trade count — bounded by `risk.py` either
  way. Midday it may revise the plan once.
- Intraday, `decide()` executes the plan mechanically on every poll: opening
  range breakouts filtered by VWAP, ATR-based stops and targets, and
  unconditional end-of-day flattening. No LLM in this loop.
- `rules` mode is the same engine with the default plan — the benchmark the
  LLM's plans have to beat to justify their token cost.

Every order — planned, mechanical, or panicked — passes `risk.check_order`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .risk import RiskLimits, check_order


@dataclass
class Decision:
    symbol: str
    action: str          # "BUY" | "SELL" | "HOLD"
    quantity: int = 0
    rationale: str = ""


@dataclass
class DayPlan:
    """What Tier 3 (the LLM) actually decides, once or twice a day.
    The default values ARE the rules-mode benchmark plan."""
    # Symbols eligible for entries today (exits always allowed on anything held).
    symbols: list[str] = field(default_factory=list)   # empty = all watched
    # Per-symbol bias: "long" (default) or "off" (no entries in that name).
    bias: dict = field(default_factory=dict)
    # Fraction of equity risked per trade (entry-to-stop distance, not notional).
    per_trade_risk_pct: float = 0.005
    # Stop and target as ATR multiples from entry.
    stop_atr: float = 1.5
    target_atr: float = 2.5
    # Exit longs that lose VWAP by this fraction (momentum failed).
    vwap_fail_pct: float = 0.002
    rationale: str = "default mechanical plan (benchmark)"


@dataclass
class SessionContext:
    day_open_equity: float
    limits: RiskLimits = field(default_factory=RiskLimits)
    plan: DayPlan = field(default_factory=DayPlan)
    # 0.0 at the open, 1.0 at the close; the runner advances this.
    session_pct: float = 0.0
    trades_today: int = 0


def flatten_all(account: dict, reason: str) -> list[Decision]:
    """SELL every share held. Called unconditionally at end of day; also the
    right response to anything deeply weird."""
    return [
        Decision(p["symbol"], "SELL", int(p["quantity"]), f"flatten: {reason}")
        for p in account.get("positions", [])
        if int(p.get("quantity", 0)) > 0
    ]


def decide(snapshot: dict, ctx: SessionContext) -> list[Decision]:
    """Mechanical intraday pass over a poller snapshot, executing ctx.plan.

    Entry: price breaks above the opening range high while holding above
           VWAP (participation confirms the break). Risk-based sizing:
           (per_trade_risk_pct * equity) / (entry - stop).
    Exit:  ATR stop, ATR target, or a close below VWAP (thesis failed).
    Late-session entries are refused by risk.py; the runner flattens at EOD.
    """
    account = snapshot["account"]
    quotes = snapshot["quotes"]
    indicators = snapshot["indicators"]
    equity = float(account.get("equity", 0.0))
    positions = {p["symbol"]: p for p in account.get("positions", [])}
    plan = ctx.plan
    decisions: list[Decision] = []

    for symbol, ind in indicators.items():
        quote = quotes.get(symbol) or {}
        last = quote.get("last")
        if last is None:
            continue
        vwap = ind.get("vwap")
        range_high = ind.get("range_high")
        range_low = ind.get("range_low")
        atr = ind.get("atr14")

        pos = positions.get(symbol)
        if pos and int(pos.get("quantity", 0)) > 0:
            avg = float(pos.get("averagePrice", 0.0)) or last
            stop = avg - plan.stop_atr * atr if atr else avg * 0.99
            target = avg + plan.target_atr * atr if atr else avg * 1.02
            qty = int(pos["quantity"])
            if last <= stop:
                decisions.append(Decision(symbol, "SELL", qty,
                                          f"ATR stop: {last:.2f} <= {stop:.2f}"))
            elif last >= target:
                decisions.append(Decision(symbol, "SELL", qty,
                                          f"ATR target: {last:.2f} >= {target:.2f}"))
            elif vwap and last < vwap * (1 - plan.vwap_fail_pct):
                decisions.append(Decision(symbol, "SELL", qty,
                                          f"lost VWAP: {last:.2f} < {vwap:.2f}"))
            continue

        # --- entries: opening range breakout confirmed by VWAP ---
        if plan.symbols and symbol not in plan.symbols:
            continue
        if plan.bias.get(symbol) == "off":
            continue
        if not (range_high and range_low and vwap and atr):
            continue
        if last > range_high and last > vwap:
            stop = max(range_low, last - plan.stop_atr * atr)
            risk_per_share = last - stop
            if risk_per_share <= 0:
                continue
            qty = int((plan.per_trade_risk_pct * equity) // risk_per_share)
            # A tight stop makes the risk-based size huge; respect the
            # notional caps we know risk.py will enforce anyway.
            notional_cap = min(ctx.limits.max_order_pct,
                               ctx.limits.max_position_pct) * equity
            qty = min(qty, int(notional_cap // last))
            if qty > 0:
                decisions.append(Decision(
                    symbol, "BUY", qty,
                    f"ORB: {last:.2f} > range high {range_high:.2f}, "
                    f"above VWAP {vwap:.2f}; stop {stop:.2f}, "
                    f"risking {plan.per_trade_risk_pct:.2%} of equity"))
    return decisions


def execute(decisions: list[Decision], snapshot: dict, ctx: SessionContext,
            client, ledger) -> list[dict]:
    """Risk-gate and place orders. Identical path for planned, mechanical,
    and flatten decisions — there is no code path around the gate."""
    account = snapshot["account"]
    quotes = snapshot["quotes"]
    results: list[dict] = []

    for d in decisions:
        if d.action == "HOLD" or d.quantity <= 0:
            continue
        quote = quotes.get(d.symbol) or {}
        est_price = quote.get("ask") if d.action == "BUY" else quote.get("bid")
        if not est_price:
            ledger.record("risk_reject", {"symbol": d.symbol, "action": d.action,
                                          "reason": "no quote available"})
            continue

        verdict = check_order(account, d.symbol, d.action, d.quantity, est_price,
                              day_open_equity=ctx.day_open_equity, limits=ctx.limits,
                              trades_today=ctx.trades_today,
                              session_pct=ctx.session_pct)
        if not verdict:
            ledger.record("risk_reject", {"symbol": d.symbol, "action": d.action,
                                          "quantity": d.quantity, "reason": verdict.reason,
                                          "rationale": d.rationale})
            continue

        order = client.place_order(d.symbol, d.action, d.quantity, "MARKET")
        entry = {"symbol": d.symbol, "action": d.action, "quantity": d.quantity,
                 "order": order, "rationale": d.rationale}
        if order.get("status") == "FILLED":
            fill_price = float(order.get("fillPrice") or 0.0)
            ledger.record("fill", {**entry, "notional": fill_price * d.quantity})
            ctx.trades_today += 1
            # keep the local view of the account current between decisions
            account = client.account()
        else:
            ledger.record("order_" + str(order.get("status", "unknown")).lower(), entry)
        results.append(entry)
    return results
