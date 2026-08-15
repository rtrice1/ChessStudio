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

from .events import entry_blocked
from .risk import RiskLimits, check_order, is_option


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
    # "shares" or "calls" — with "calls", breakout signals are expressed by
    # buying near-the-money 0DTE calls instead of stock. LONG ONLY, always.
    instrument: str = "shares"
    # The position budget: at most this many concurrent positions, and at
    # most this many new entries per cycle. When more signals fire than
    # slots exist, score_entry() ranks them and only the best trade.
    max_positions: int = 4
    max_entries_per_cycle: int = 2
    # Premium spent per options entry, as a fraction of equity. This is the
    # full amount at risk — a 0DTE option can and does go to zero.
    premium_per_trade_pct: float = 0.01
    # Premium exits: stop at this fraction of entry premium, target at that
    # multiple of it. Wide on purpose — option noise is huge.
    premium_stop_frac: float = 0.5
    premium_target_mult: float = 2.0
    rationale: str = "default mechanical plan (benchmark)"


@dataclass
class SessionContext:
    day_open_equity: float
    limits: RiskLimits = field(default_factory=RiskLimits)
    plan: DayPlan = field(default_factory=DayPlan)
    # 0.0 at the open, 1.0 at the close; the runner advances this.
    session_pct: float = 0.0
    trades_today: int = 0
    # The gut's read on the day, set by the runner after its gut check.
    # Shades entry scoring (chop makes chasing costlier); never gates.
    hunch: dict | None = None
    # Active scheduled-event blackouts (events.Blackout), set by the runner
    # each cycle. Blocks ENTRIES only — exits always work.
    blackouts: list = field(default_factory=list)


def score_entry(ind: dict, news: dict | None = None,
                hunch: dict | None = None) -> tuple[float, str]:
    """Rank an entry candidate: momentum confluence, shaded by news and gut.

    Signals fire more often than the position budget allows, so every
    candidate gets a score and only the best trade. The weights are
    deliberate: trend strength (ADX) and participation (relative volume)
    carry the most; news carries little and asymmetrically — the desk
    belief is that news misleads, so bad news subtracts more than good
    news adds; the gut hunch doesn't add points, it changes what gets
    penalized (in suspected chop, weak trends and overextension cost more).
    """
    score = 0.0
    reasons: list[str] = []
    chop = bool(hunch
                and hunch.get("suspected_day_type") in ("chop", "open_spike_settle")
                and float(hunch.get("confidence") or 0) >= 0.5
                and int(hunch.get("based_on") or 0) >= 3)

    adx = ind.get("adx")
    if adx is not None:
        if adx >= 25:
            score += 1.0
            reasons.append(f"adx {adx:.0f} trending")
        elif adx >= 20:
            score += 0.5
            reasons.append(f"adx {adx:.0f} building")
        elif chop:
            score -= 0.5
            reasons.append(f"adx {adx:.0f} weak + chop suspected")

    plus_di, minus_di = ind.get("plus_di"), ind.get("minus_di")
    if plus_di is not None and minus_di is not None and plus_di > minus_di:
        score += 0.5
        reasons.append("+DI>-DI")

    macd_hist = ind.get("macd_hist")
    if macd_hist is not None:
        if macd_hist > 0:
            score += 1.0
            reasons.append("macd+")
        else:
            score -= 0.5
            reasons.append("macd-")

    rel_volume = ind.get("rel_volume")
    if rel_volume is not None:
        if rel_volume >= 1.5:
            score += 1.0
            reasons.append(f"rvol {rel_volume:.1f}")
        elif rel_volume >= 1.0:
            score += 0.5
            reasons.append(f"rvol {rel_volume:.1f}")
        else:
            score -= 0.5
            reasons.append(f"rvol {rel_volume:.1f} thin")

    percent_b = ind.get("bb_percent_b")
    if percent_b is not None:
        if percent_b > 1.05:
            score -= 1.0 if chop else 0.5
            reasons.append(f"%B {percent_b:.2f} overextended")
        elif percent_b >= 0.8:
            score += 0.5
            reasons.append(f"%B {percent_b:.2f} riding band")

    rsi14 = ind.get("rsi14")
    if rsi14 is not None:
        if rsi14 >= 75:
            score -= 1.0
            reasons.append(f"rsi {rsi14:.0f} overbought")
        elif rsi14 >= 50:
            score += 0.5
            reasons.append(f"rsi {rsi14:.0f} has room")

    roc10 = ind.get("roc10")
    if roc10 is not None and roc10 > 0:
        score += 0.25
        reasons.append(f"roc {roc10:+.1f}")

    if news:
        sent = (int(news.get("wire_sentiment") or 0)
                + int(news.get("board_sentiment") or 0))
        if sent < 0:
            score -= 0.75
            reasons.append(f"news {sent:+d}")
        elif sent > 0:
            score += 0.25
            reasons.append(f"news {sent:+d}")

    return score, ", ".join(reasons) if reasons else "no confluence data"


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
           VWAP (participation confirms the break). Every trigger is then
           SCORED — momentum confluence shaded by news and gut — and only
           the top-ranked fit under the plan's position budget: at most
           max_entries_per_cycle new entries, never holding more than
           max_positions names. Risk-based sizing:
           (per_trade_risk_pct * equity) / (entry - stop).
    Exit:  ATR stop, ATR target, or a close below VWAP (thesis failed).
           Exits are never budgeted, ranked, or deferred.
    Late-session entries are refused by risk.py; the runner flattens at EOD.
    """
    account = snapshot["account"]
    quotes = snapshot["quotes"]
    indicators = snapshot["indicators"]
    news_summary = (snapshot.get("news") or {}).get("summary") or {}
    equity = float(account.get("equity", 0.0))
    positions = {p["symbol"]: p for p in account.get("positions", [])}
    plan = ctx.plan
    decisions: list[Decision] = []
    candidates: list[tuple[float, Decision]] = []
    held_count = sum(1 for p in account.get("positions", [])
                     if int(p.get("quantity", 0)) > 0)

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
            # A halted name has a frozen, untrustworthy last — no exit
            # decision would fill anyway, and acting on the stale print
            # would be acting on fiction. Wait for the resume.
            if quote.get("halted"):
                continue
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
        if quote.get("halted"):
            continue
        # Known event time in window (FOMC, CPI, earnings): stand flat
        # into it. The reaction is tradeable; the print is a coin toss.
        if entry_blocked(ctx.blackouts, symbol):
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
                score, why = score_entry(ind, news_summary.get(symbol),
                                         ctx.hunch)
                candidates.append((score, Decision(
                    symbol, "BUY", qty,
                    f"ORB: {last:.2f} > range high {range_high:.2f}, "
                    f"above VWAP {vwap:.2f}; stop {stop:.2f}, "
                    f"risking {plan.per_trade_risk_pct:.2%} of equity"
                    f" | score {score:+.2f} ({why})")))

    # --- the position budget: rank the triggers, trade only the best ---
    slots = max(0, min(plan.max_entries_per_cycle,
                       plan.max_positions - held_count))
    candidates.sort(key=lambda c: c[0], reverse=True)
    for rank, (score, d) in enumerate(candidates[:slots], start=1):
        if len(candidates) > slots:
            d.rationale += f" | won slot {rank}/{slots} over {len(candidates) - slots} rivals"
        decisions.append(d)
    return decisions


def pick_call(chain: dict, target_delta: float = 0.55) -> dict | None:
    """Nearest-the-money call by delta from a chain payload."""
    calls = [c for c in (chain.get("calls") or [])
             if c.get("ask", 0) > 0 and c.get("delta") is not None]
    if not calls:
        return None
    return min(calls, key=lambda c: abs(float(c["delta"]) - target_delta))


def translate_to_calls(decision: Decision, client, equity: float,
                       plan: DayPlan) -> Decision | None:
    """Express a stock breakout signal as a long call purchase.

    The signal machinery is unchanged — same ORB+VWAP trigger — only the
    instrument differs. Sizing is by premium budget: contracts = budget //
    per-contract cost, and the whole budget is the amount at risk.
    """
    chain = client.chain(decision.symbol)
    if not chain or chain.get("error"):
        return None
    call = pick_call(chain)
    if call is None:
        return None
    per_contract = float(call["ask"]) * 100.0  # chain premiums are per share
    budget = plan.premium_per_trade_pct * equity
    contracts = int(budget // per_contract)
    if contracts < 1:
        return None
    return Decision(call["contractSymbol"], "BUY", contracts,
                    f"{decision.rationale} | as calls: {call['contractSymbol']} "
                    f"delta {float(call['delta']):.2f}, premium "
                    f"{per_contract:.2f}/contract x{contracts} "
                    f"(risking {plan.premium_per_trade_pct:.1%} of equity)")


def option_exits(account: dict, client, plan: DayPlan) -> list[Decision]:
    """Premium-based stops and targets for held contracts. A long option
    that loses half its premium is a broken trade; one that doubles gets
    taken. Everything left is flattened at end of day like any position."""
    decisions: list[Decision] = []
    for p in account.get("positions", []):
        symbol = p.get("symbol", "")
        qty = int(p.get("quantity", 0))
        if qty <= 0 or not is_option(symbol):
            continue
        quote = (client.quotes([symbol]) or {}).get(symbol) or {}
        bid = float(quote.get("bid") or 0.0)
        avg = float(p.get("averagePrice") or 0.0)
        if avg <= 0 or bid <= 0:
            continue
        if bid <= plan.premium_stop_frac * avg:
            decisions.append(Decision(symbol, "SELL", qty,
                                      f"premium stop: {bid:.2f} <= "
                                      f"{plan.premium_stop_frac:.0%} of {avg:.2f}"))
        elif bid >= plan.premium_target_mult * avg:
            decisions.append(Decision(symbol, "SELL", qty,
                                      f"premium target: {bid:.2f} >= "
                                      f"{plan.premium_target_mult:.1f}x {avg:.2f}"))
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
        # Plan says calls: express stock BUY signals as long-call purchases.
        if (ctx.plan.instrument == "calls" and d.action == "BUY"
                and not is_option(d.symbol)):
            translated = translate_to_calls(d, client,
                                            float(account.get("equity", 0.0)),
                                            ctx.plan)
            if translated is None:
                ledger.record("risk_reject", {"symbol": d.symbol, "action": "BUY",
                                              "reason": "no viable call contract"})
                continue
            d = translated
        if is_option(d.symbol):
            quote = (client.quotes([d.symbol]) or {}).get(d.symbol) or {}
        else:
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
            # The cost of crossing the spread, measured on every fill:
            # slippage vs mid is what a marketable limit at mid would have
            # saved. On 40 trades/day the spread IS the P&L — metrics.py
            # aggregates this into the scoreboard's "spread paid".
            bid, ask = quote.get("bid"), quote.get("ask")
            slippage = None
            if bid and ask and fill_price:
                mid = (float(bid) + float(ask)) / 2.0
                per_share = (fill_price - mid if d.action == "BUY"
                             else mid - fill_price)
                mult = 100.0 if is_option(d.symbol) else 1.0
                slippage = {"vs_mid_per_share": round(per_share, 4),
                            "vs_mid_total": round(per_share * d.quantity * mult, 2),
                            "half_spread": round((float(ask) - float(bid)) / 2.0, 4)}
            ledger.record("fill", {**entry, "notional": fill_price * d.quantity,
                                   "slippage": slippage})
            # The daily cap limits ENTRIES; exits must never consume it
            # (a desk that can't sell because it bought too much today
            # would be trapped in its own positions).
            if d.action == "BUY":
                ctx.trades_today += 1
            # keep the local view of the account current between decisions
            account = client.account()
        else:
            ledger.record("order_" + str(order.get("status", "unknown")).lower(), entry)
        results.append(entry)
    return results
