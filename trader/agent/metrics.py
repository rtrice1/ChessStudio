"""The desk scoreboard — every number computed from records we already keep.

Principles:
- Metrics are derived, never stored: the ledgers and journal are the
  source of truth, and the scoreboard recomputes from them so it can
  never drift from the records.
- Per-trade beats per-day: daily P&L hides whether the edge is real.
  Expectancy (win rate x avg win - loss rate x avg loss) and profit
  factor are the honest summary; forty small losses and two big wins
  look identical to the reverse in a daily number.
- The judgment layer gets measured too: hunch calibration (was the
  11:00 gut call right about the day?) and the token cost of judgment
  sit next to the trading numbers, because "is Tier 3 paying for
  itself" is a question this file must eventually answer.

CLI: cd trader && python -m agent.metrics
"""
from __future__ import annotations

import json
import os
import statistics
from collections import Counter, deque


def round_trips(fills: list[dict]) -> list[dict]:
    """FIFO-pair BUY/SELL fills into closed round trips per symbol.

    Each fill payload carries symbol, action, quantity, and order.fillPrice.
    Returns [{"symbol", "quantity", "entry", "exit", "pnl"}...] in exit order.
    """
    lots: dict[str, deque] = {}
    trips: list[dict] = []
    for fill in fills:
        symbol = fill.get("symbol")
        qty = int(fill.get("quantity", 0))
        price = float(((fill.get("order") or {}).get("fillPrice")) or 0.0)
        if not symbol or qty <= 0 or price <= 0:
            continue
        if fill.get("action") == "BUY":
            lots.setdefault(symbol, deque()).append([qty, price])
        elif fill.get("action") == "SELL":
            queue = lots.setdefault(symbol, deque())
            remaining = qty
            while remaining > 0 and queue:
                lot = queue[0]
                take = min(remaining, lot[0])
                trips.append({"symbol": symbol, "quantity": take,
                              "entry": lot[1], "exit": price,
                              "pnl": round((price - lot[1]) * take, 4)})
                lot[0] -= take
                remaining -= take
                if lot[0] == 0:
                    queue.popleft()
    return trips


def trade_stats(trips: list[dict]) -> dict:
    """Expectancy, win rate, payoff, profit factor over closed round trips."""
    if not trips:
        return {"trades": 0}
    pnls = [t["pnl"] for t in trips]
    wins = [p for p in pnls if p > 0]
    losses = [-p for p in pnls if p < 0]
    win_rate = len(wins) / len(pnls)
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    gross_win, gross_loss = sum(wins), sum(losses)
    return {
        "trades": len(pnls),
        "win_rate": round(win_rate, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "payoff_ratio": round(avg_win / avg_loss, 3) if avg_loss else None,
        "expectancy_per_trade": round(
            win_rate * avg_win - (1 - win_rate) * avg_loss, 4),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "net_pnl": round(sum(pnls), 2),
    }


def daily_stats(days: list[dict]) -> dict:
    """Over journal trading_day entries: consistency, drawdown, invariants."""
    traded = [d for d in days if d.get("kind") == "trading_day"]
    if not traded:
        return {"days": 0}
    rets = [float(d.get("pnl_pct") or 0.0) for d in traded]
    equity, peak, max_dd = 1.0, 1.0, 0.0
    for r in rets:
        equity *= (1 + r)
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)
    out = {
        "days": len(traded),
        "mean_daily_return": round(statistics.mean(rets), 6),
        "worst_day": round(min(rets), 6),
        "best_day": round(max(rets), 6),
        "cumulative_return": round(equity - 1.0, 6),
        "max_drawdown": round(max_dd, 6),
        # THE invariant: a day trader ends every day in cash.
        "flat_at_close_rate": round(
            sum(1 for d in traded if d.get("flat_at_close")) / len(traded), 4),
        "daily_stops_hit": sum(1 for d in traded if d.get("daily_stop_hit")),
    }
    if len(rets) >= 2 and statistics.pstdev(rets) > 0:
        out["sharpe_like"] = round(
            statistics.mean(rets) / statistics.pstdev(rets), 3)
    return out


def hunch_calibration(ledger_entries: list[dict], days: list[dict]) -> dict:
    """Grade the 11:00 hunch against the end-of-day classification.

    A gut that's 40% accurate at confidence 0.7 needs to know that about
    itself; this is where it finds out. Matched by calendar date.
    """
    label_by_date = {d["ts"][:10]: d.get("day_type") for d in days
                     if d.get("kind") == "trading_day" and d.get("day_type")}
    graded = []
    for entry in ledger_entries:
        if entry.get("kind") != "gut_check":
            continue
        hunch = (entry.get("hunch") or {})
        suspected = hunch.get("suspected_day_type")
        actual = label_by_date.get(entry.get("ts", "")[:10])
        if suspected and actual:
            graded.append({"suspected": suspected, "actual": actual,
                           "confidence": hunch.get("confidence", 0.0),
                           "hit": suspected == actual})
    if not graded:
        return {"graded": 0}
    hits = [g for g in graded if g["hit"]]
    return {
        "graded": len(graded),
        "accuracy": round(len(hits) / len(graded), 4),
        "mean_confidence": round(
            statistics.mean(g["confidence"] for g in graded), 4),
        # calibration gap > 0 = overconfident, < 0 = underconfident
        "calibration_gap": round(
            statistics.mean(g["confidence"] for g in graded)
            - len(hits) / len(graded), 4),
        "confusion": dict(Counter(f"{g['suspected']}->{g['actual']}"
                                  for g in graded if not g["hit"])),
    }


def reject_histogram(ledger_entries: list[dict]) -> dict:
    """Which risk rules actually bind. The first word of each reason is a
    stable-enough key (daily trade cap, entry cutoff, position, gross...)."""
    reasons = Counter()
    for entry in ledger_entries:
        if entry.get("kind") == "risk_reject":
            reason = str(entry.get("reason", "?"))
            reasons[" ".join(reason.split()[:3])] += 1
    return dict(reasons.most_common(10))


def day_trades_last_sessions(ledger_path: str, sessions: int = 5) -> int:
    """SELL fills across the last N sessions (session_start markers),
    current session included. Flat-by-close makes every SELL a completed
    day trade, so this is the number the FINRA PDT rule counts."""
    entries = _read_jsonl(ledger_path)
    starts = [i for i, e in enumerate(entries)
              if e.get("kind") in ("session_start", "live_session_start")]
    if not starts:
        return 0
    cut = starts[-sessions] if len(starts) >= sessions else starts[0]
    return sum(1 for e in entries[cut:]
               if e.get("kind") == "fill" and e.get("action") == "SELL")


def slippage_stats(fills: list[dict]) -> dict:
    """What crossing the spread cost, aggregated from per-fill measurements.

    `total_vs_mid` is the dollars a marketable limit at mid would have
    saved across all fills — the number that decides whether building
    smarter order routing is worth it. Fills recorded before slippage
    measurement existed simply don't count toward the sample.
    """
    slips = [f["slippage"] for f in fills
             if isinstance(f.get("slippage"), dict)
             and f["slippage"].get("vs_mid_total") is not None]
    if not slips:
        return {"measured_fills": 0}
    totals = [float(s["vs_mid_total"]) for s in slips]
    return {
        "measured_fills": len(slips),
        "total_vs_mid": round(sum(totals), 2),
        "avg_per_fill": round(sum(totals) / len(slips), 2),
        "avg_half_spread": round(
            sum(float(s.get("half_spread") or 0.0) for s in slips) / len(slips), 4),
    }


def judgment_cost(token_entries: list[dict]) -> dict:
    """What the LLM tiers cost — the number plan-alpha must beat."""
    spent = [e for e in token_entries if e.get("status") in ("ok", "dry_run")]
    if not spent:
        return {"invocations": 0}
    return {
        "invocations": len(spent),
        "est_cost_usd": round(sum(e.get("est_cost_usd", 0.0) for e in spent), 4),
        "refusals_and_denials": len(
            [e for e in token_entries
             if e.get("status") in ("budget_denied", "refusal")]),
        "by_kind": dict(Counter(e.get("kind", "?") for e in spent)),
    }


def _read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def scoreboard(data_dir: str, desk_dir: str) -> dict:
    """Assemble the full scoreboard from the ledgers and journal on disk."""
    ledger = _read_jsonl(os.path.join(data_dir, "ledger.jsonl"))
    journal = _read_jsonl(os.path.join(desk_dir, "journal.jsonl"))
    tokens = _read_jsonl(os.path.join(data_dir, "token_ledger.jsonl"))
    fills = [e for e in ledger if e.get("kind") == "fill"]
    return {
        "per_trade": trade_stats(round_trips(fills)),
        "per_day": daily_stats(journal),
        "slippage": slippage_stats(fills),
        "hunch_calibration": hunch_calibration(ledger, journal),
        "risk_rejects": reject_histogram(ledger),
        "judgment_cost": judgment_cost(tokens),
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--data-dir", default=os.path.join(base, "data"))
    ap.add_argument("--desk-dir", default=os.path.join(base, "desk_state"))
    args = ap.parse_args()
    print(json.dumps(scoreboard(args.data_dir, args.desk_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
