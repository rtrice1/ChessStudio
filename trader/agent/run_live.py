"""Run one real trading day in shadow mode: real Schwab data, local fills.

This is the Monday entry point. Wall-clock, market-hours aware (America/
New_York), and structurally incapable of routing a real order: market data
comes from SchwabClient (read-only by construction) and every order fills
in the ShadowBroker's local book at real quotes.

    python -m agent.run_live                    # shadow day on real data
    python -m agent.run_live --instrument calls # express entries as calls
    python -m agent.run_live --once             # one poll/decide cycle, then exit

The same discipline as the sim: entry cutoff late in the session,
daily-loss breaker flattens and ends the day, unconditional flatten at
15:45 ET, post-mortem journal + gut memory at the close.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.daytype import classify_day, features_from_client  # noqa: E402
from agent.desk import Desk                     # noqa: E402
from agent.gut import Gut                       # noqa: E402
from agent.ledger import Ledger                 # noqa: E402
from agent.poller import poll_once              # noqa: E402
from agent.schwab import SchwabClient, TokenStore  # noqa: E402
from agent.shadow import ShadowBroker           # noqa: E402
from agent.stream import StreamingDataFeed      # noqa: E402
from agent.strategist import (                  # noqa: E402
    DayPlan, SessionContext, decide, execute, flatten_all, option_exits,
)

ET = ZoneInfo("America/New_York")
OPEN_T, CLOSE_T = dtime(9, 30), dtime(16, 0)
FLATTEN_T = dtime(15, 45)
SYMBOLS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
           "SPY", "QQQ", "TSLA", "JPM", "XOM"]


def session_pct(now_et: datetime) -> float:
    open_s = now_et.replace(hour=9, minute=30, second=0).timestamp()
    close_s = now_et.replace(hour=16, minute=0, second=0).timestamp()
    return min(1.0, max(0.0, (now_et.timestamp() - open_s) / (close_s - open_s)))


def load_plan(data_dir: str, instrument_override: str | None) -> DayPlan:
    """Use the harness-written day plan when one exists for today."""
    path = os.path.join(data_dir, "day_plan.json")
    plan = DayPlan()
    if os.path.exists(path):
        try:
            from agent.harness import apply_day_plan
            with open(path, encoding="utf-8") as f:
                plan = apply_day_plan(json.load(f), data_dir)
        except Exception as exc:
            print(f"day_plan.json unusable ({exc}); using default plan")
    if instrument_override:
        plan.instrument = instrument_override
    return plan


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--data-dir", default=os.path.join(base, "data"))
    ap.add_argument("--desk-dir", default=os.path.join(base, "desk_state"))
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--starting-cash", type=float, default=100_000.0)
    ap.add_argument("--instrument", choices=["shares", "calls"], default=None)
    ap.add_argument("--once", action="store_true",
                    help="one cycle then exit (plumbing check outside hours)")
    ap.add_argument("--no-stream", action="store_true",
                    help="REST polling only (default: stream when available)")
    args = ap.parse_args()

    data = StreamingDataFeed(SchwabClient(TokenStore()), SYMBOLS,
                             enable_stream=not args.no_stream)
    broker = ShadowBroker(data, SYMBOLS, starting_cash=args.starting_cash,
                          book_path=os.path.join(args.data_dir, "shadow_book.json"))
    ledger = Ledger(os.path.join(args.data_dir, "ledger.jsonl"))
    desk = Desk(args.desk_dir)
    gut = Gut(os.path.join(args.desk_dir, "day_memory.jsonl"))

    start = broker.account()
    ctx = SessionContext(day_open_equity=float(start["equity"]),
                         plan=load_plan(args.data_dir, args.instrument))
    ledger.record("live_session_start", {
        "mode": "shadow", "equity": start["equity"],
        "instrument": ctx.plan.instrument, "plan": ctx.plan.rationale})
    print(f"SHADOW day | equity {start['equity']:.2f} | "
          f"instrument {ctx.plan.instrument} | plan: {ctx.plan.rationale[:80]}")

    day_stopped = flattened = gut_checked = False
    while True:
        now = datetime.now(ET)
        if not args.once:
            if now.time() < OPEN_T:
                time.sleep(min(60, args.interval))
                continue
            if now.time() >= CLOSE_T:
                break
        ctx.session_pct = session_pct(now)
        try:
            snapshot = poll_once(broker, SYMBOLS, args.data_dir)
            # One gut check after the opening range forms: the day's
            # fingerprint shades entry scoring for the rest of the session.
            if not gut_checked and ctx.session_pct >= 0.10:
                gut_checked = True
                features = features_from_client(broker, SYMBOLS)
                if features:
                    ctx.hunch = gut.hunch(features)
                    ledger.record("gut_check", {"features": features,
                                                "hunch": ctx.hunch})
                    print(f"{now:%H:%M} | gut check: {ctx.hunch['note']}")
            equity = float(snapshot["account"].get("equity", 0.0))
            drawdown = (ctx.day_open_equity - equity) / ctx.day_open_equity
            if not day_stopped and drawdown >= ctx.limits.daily_loss_halt_pct:
                day_stopped = True
                ledger.record("daily_stop", {"drawdown": round(drawdown, 6)})
                print(f"{now:%H:%M} | DAILY STOP: down {drawdown:.2%}")
            if day_stopped or now.time() >= FLATTEN_T:
                decisions = flatten_all(snapshot["account"],
                                        "daily max loss" if day_stopped
                                        else "end of day")
                flattened = flattened or bool(decisions)
            else:
                decisions = decide(snapshot, ctx)
                decisions += option_exits(snapshot["account"], broker, ctx.plan)
            executed = execute(decisions, snapshot, ctx, broker, ledger)
            if executed:
                # Stream quotes for any contracts we just opened.
                opened = [e["symbol"] for e in executed
                          if e["action"] == "BUY" and len(e["symbol"]) > 10]
                if opened:
                    data.subscribe_options(opened)
                for e in executed:
                    print(f"{now:%H:%M} | {e['action']} {e['quantity']}x "
                          f"{e['symbol']} | {e['rationale'][:70]}")
            elif int(now.timestamp() / args.interval) % 10 == 0:
                print(f"{now:%H:%M} | {ctx.session_pct:4.0%} | "
                      f"equity {equity:.2f} | trades {ctx.trades_today}")
        except Exception as exc:
            print(f"{now:%H:%M} | ERROR: {exc}", file=sys.stderr)
        if args.once:
            break
        time.sleep(args.interval)

    final = broker.account()
    open_pos = [p for p in final["positions"] if p["quantity"]]
    pnl = float(final["equity"]) - float(start["equity"])
    day_type = None
    features = features_from_client(broker, SYMBOLS)
    if features:
        classification = classify_day(features)
        day_type = classification["day_type"]
        gut.record_day(features, day_type,
                       {"pnl_pct": round(pnl / float(start["equity"]), 6),
                        "trades": ctx.trades_today, "source": "shadow_live",
                        "daily_stop_hit": day_stopped})
    desk.journal_append("trading_day", {
        "mode": "shadow", "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / float(start["equity"]), 6),
        "trades": ctx.trades_today, "flat_at_close": not open_pos,
        "daily_stop_hit": day_stopped, "day_type": day_type,
        "instrument": ctx.plan.instrument, "plan": ctx.plan.rationale})
    broker.save()
    if hasattr(data, "stats"):
        ledger.record("stream_stats", data.stats())
        print(f"stream    | {data.stats()}")
    print(f"SHADOW close | equity {final['equity']:.2f} | P&L {pnl:+.2f} | "
          f"trades {ctx.trades_today} | flat: {not open_pos} | "
          f"day type: {day_type}")
    return 0 if (not open_pos or args.once) else 1


if __name__ == "__main__":
    raise SystemExit(main())
