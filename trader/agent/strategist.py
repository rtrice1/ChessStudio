"""Tier-3 decision engine.

This module has two modes:

1. `rules` mode (implemented here): a deliberately boring mean-reversion +
   trend-filter baseline that runs with no LLM at all. It exists so the
   plumbing can be exercised end-to-end and so there is always a benchmark
   the LLM strategist has to beat to justify its token cost.

2. `llm` mode (the actual point): the snapshot produced by the Tier-1 poller
   is compact and structured precisely so it can be pasted into a Claude
   invocation. The strategist prompt contract lives in SPEC/AGENTS.md; the
   decision schema is `Decision` below. When run under the agent harness,
   `decide()` is replaced by a Claude call that returns the same schema —
   the risk gate and executor code paths are identical either way, which is
   the whole safety story: the model proposes, this code disposes.
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
class SessionContext:
    day_open_equity: float
    limits: RiskLimits = field(default_factory=RiskLimits)


def decide(snapshot: dict, ctx: SessionContext) -> list[Decision]:
    """Baseline rules strategy over a poller snapshot.

    Entry: RSI(14) < 32 (washed out) while price holds above SMA20 * 0.97
           (not a falling knife) -> buy a ~5%-of-equity slug.
    Exit:  RSI(14) > 68, or position down 4% from average price (stop),
           or up 6% (take profit).
    """
    account = snapshot["account"]
    quotes = snapshot["quotes"]
    indicators = snapshot["indicators"]
    equity = float(account.get("equity", 0.0))
    positions = {p["symbol"]: p for p in account.get("positions", [])}
    decisions: list[Decision] = []

    for symbol, ind in indicators.items():
        quote = quotes.get(symbol) or {}
        last = quote.get("last")
        rsi = ind.get("rsi14")
        sma20 = ind.get("sma20")
        if last is None or rsi is None:
            continue

        pos = positions.get(symbol)
        if pos and int(pos.get("quantity", 0)) > 0:
            avg = float(pos.get("averagePrice", 0.0)) or last
            ret = (last - avg) / avg
            if ret <= -0.04:
                decisions.append(Decision(symbol, "SELL", int(pos["quantity"]),
                                          f"stop loss: {ret:.2%} from avg {avg:.2f}"))
            elif ret >= 0.06:
                decisions.append(Decision(symbol, "SELL", int(pos["quantity"]),
                                          f"take profit: {ret:.2%} from avg {avg:.2f}"))
            elif rsi > 68:
                decisions.append(Decision(symbol, "SELL", int(pos["quantity"]),
                                          f"RSI {rsi:.1f} overbought exit"))
            continue

        if rsi < 32 and sma20 and last > 0.97 * sma20:
            target_notional = 0.05 * equity
            qty = int(target_notional // last)
            if qty > 0:
                decisions.append(Decision(symbol, "BUY", qty,
                                          f"RSI {rsi:.1f} oversold, price {last:.2f} "
                                          f"holding near SMA20 {sma20:.2f}"))
    return decisions


def execute(decisions: list[Decision], snapshot: dict, ctx: SessionContext,
            client, ledger) -> list[dict]:
    """Run decisions through the risk gate, place approved orders, log everything.
    Identical path for rules mode and llm mode."""
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
                              day_open_equity=ctx.day_open_equity, limits=ctx.limits)
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
            # keep the local view of the account current between decisions
            account = client.account()
        else:
            ledger.record("order_" + str(order.get("status", "unknown")).lower(), entry)
        results.append(entry)
    return results
