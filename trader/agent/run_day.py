"""Simulate one full day-trading session against the mock API.

The session clock maps N cycles onto one 09:30–16:00 trading day:
session_pct advances each cycle, risk.py refuses new entries after its
entry cutoff, and the runner unconditionally flattens every position in
the final cycles — a day trader ends the day in cash, always.

At the end, the day is journaled to the desk (see agent/desk.py and
SPEC/PERSISTENCE.md) so the next strategist instance wakes up knowing
what happened today and why.

Usage:
    cd trader && python -m agent.run_day --cycles 78 --time-scale 300
    # 78 cycles ~= one 5-min-bar trading day
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.client import BrokerClient           # noqa: E402
from agent.daytype import classify_day, features_from_client  # noqa: E402
from agent.desk import Desk                     # noqa: E402
from agent.focus import assess, build_context, items_from_snapshot  # noqa: E402
from agent.gut import Gut                       # noqa: E402
from agent.ledger import Ledger                 # noqa: E402
from agent.poller import poll_once              # noqa: E402
from agent.strategist import (                  # noqa: E402
    DayPlan, SessionContext, decide, execute, flatten_all,
)
from mockschwab.server import create_server     # noqa: E402

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
                   "SPY", "QQQ", "TSLA", "JPM", "XOM"]
# Flatten when this fraction of the session has elapsed (~15:45 ET).
FLATTEN_AT = 0.96
# Ask the gut what today smells like once the open has resolved (~11:00 ET).
GUT_CHECK_AT = 0.25


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles", type=int, default=78,
                    help="cycles per day (78 = one 6.5h day of 5-min bars)")
    ap.add_argument("--cycle-seconds", type=float, default=0.2,
                    help="real seconds between cycles")
    ap.add_argument("--time-scale", type=float, default=1500.0,
                    help="sim seconds per real second (1500 * 0.2s = 5 sim min/cycle)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--starting-cash", type=float, default=100_000.0)
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    ap.add_argument("--desk-dir", default=os.path.join(os.path.dirname(__file__), "..", "desk_state"))
    args = ap.parse_args()

    server = create_server(port=args.port, seed=args.seed,
                           time_scale=args.time_scale,
                           starting_cash=args.starting_cash)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    client = BrokerClient(f"http://127.0.0.1:{args.port}")
    ledger = Ledger(os.path.join(args.data_dir, "ledger.jsonl"))
    desk = Desk(args.desk_dir)
    gut = Gut(os.path.join(args.desk_dir, "day_memory.jsonl"))

    start = client.account()
    # The ledger persists across days; count this session's rejects only.
    rejects_at_open = ledger.summary()["kind_counts"].get("risk_reject", 0)
    ctx = SessionContext(day_open_equity=float(start["equity"]), plan=DayPlan())
    ledger.record("session_start", {"equity": start["equity"], "seed": args.seed,
                                    "plan": ctx.plan.rationale})
    print(f"day open  | equity {start['equity']:.2f} | plan: {ctx.plan.rationale}")

    day_stopped = False
    gut_checked = False
    for cycle in range(1, args.cycles + 1):
        ctx.session_pct = cycle / args.cycles
        try:
            snapshot = poll_once(client, DEFAULT_SYMBOLS, args.data_dir)
            if not gut_checked and ctx.session_pct >= GUT_CHECK_AT:
                gut_checked = True
                features = features_from_client(client, DEFAULT_SYMBOLS)
                if features:
                    hunch = gut.hunch(features)
                    ledger.record("gut_check", {"features": features, "hunch": hunch})
                    print(f"{ctx.session_pct:5.0%} | gut check: {hunch['note']}")
                    # Build the focused context a Tier-3 invocation would get
                    # here, and record what was deliberately left out of mind.
                    state = assess(snapshot["account"], snapshot.get("alerts", []),
                                   hunch, ctx.session_pct, ctx.day_open_equity)
                    focused = build_context(
                        items_from_snapshot(snapshot, desk.load_context(), hunch),
                        state)
                    ledger.record("focus", {
                        "width": state.width, "topics": state.topics,
                        "reason": focused["state"].reason,
                        "passes": focused["passes"],
                        "included": len(focused["included"]),
                        "excluded": focused["excluded"]})
                    print(f"      | focus: width {state.width:.2f} "
                          f"({focused['state'].reason}) — "
                          f"{len(focused['included'])} in, "
                          f"{len(focused['excluded'])} left out of mind")
                    # A hunch shades the plan; it never overrides the gate.
                    if (hunch["suspected_day_type"] in ("chop", "open_spike_settle")
                            and hunch["confidence"] >= 0.5 and hunch["based_on"] >= 3):
                        ctx.plan.per_trade_risk_pct /= 2
                        ctx.plan.rationale += (
                            f" | gut: {hunch['suspected_day_type']} suspected, "
                            f"risk halved")
                        print(f"      | plan shaded: per-trade risk halved "
                              f"({hunch['suspected_day_type']})")
            equity = float(snapshot["account"].get("equity", 0.0))
            drawdown = (ctx.day_open_equity - equity) / ctx.day_open_equity
            if not day_stopped and drawdown >= ctx.limits.daily_loss_halt_pct:
                # Daily max loss: don't just stop buying — go flat and be
                # done for the day. Losses past the breaker come from
                # positions the breaker alone wouldn't have closed.
                day_stopped = True
                ledger.record("daily_stop", {"drawdown": round(drawdown, 6),
                                             "equity": equity, "cycle": cycle})
                print(f"{ctx.session_pct:5.0%} | cycle {cycle:3d} | "
                      f"DAILY STOP: down {drawdown:.2%}, flattening, done for the day")
            if day_stopped:
                decisions = flatten_all(snapshot["account"], "daily max loss")
            elif ctx.session_pct >= FLATTEN_AT:
                decisions = flatten_all(snapshot["account"], "end of day")
            else:
                decisions = decide(snapshot, ctx)
            executed = execute(decisions, snapshot, ctx, client, ledger)
            if executed or cycle % 10 == 0 or cycle == args.cycles:
                account = client.account()
                npos = len([p for p in account["positions"] if p["quantity"]])
                print(f"{ctx.session_pct:5.0%} | cycle {cycle:3d} | "
                      f"equity {account['equity']:>10.2f} | positions {npos} | "
                      f"trades today {ctx.trades_today} | "
                      f"executed {len(executed)}")
        except Exception as exc:  # keep the day alive through single-cycle failures
            print(f"cycle {cycle:3d} | ERROR: {exc}", file=sys.stderr)
        time.sleep(args.cycle_seconds)

    final = client.account()
    open_pos = [p for p in final["positions"] if p["quantity"]]
    pnl = float(final["equity"]) - float(start["equity"])
    summary = ledger.summary()

    # Fingerprint the finished day and commit it to gut memory.
    day_type = None
    features = features_from_client(client, DEFAULT_SYMBOLS)
    if features:
        classification = classify_day(features)
        day_type = classification["day_type"]
        gut.record_day(features, day_type,
                       {"pnl_pct": round(pnl / float(start["equity"]), 6),
                        "trades": ctx.trades_today,
                        "daily_stop_hit": day_stopped})
        print(f"day type  | {day_type} "
              f"(confidence {classification['confidence']}) -> gut memory")
    ledger.record("session_end", {"equity": final["equity"], "pnl": pnl,
                                  "trades": ctx.trades_today,
                                  "flat": not open_pos,
                                  "ledger_summary": summary})
    desk.journal_append("trading_day", {
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / float(start["equity"]), 6),
        "trades": ctx.trades_today,
        "flat_at_close": not open_pos,
        "plan": ctx.plan.rationale,
        "risk_rejects": summary["kind_counts"].get("risk_reject", 0) - rejects_at_open,
        "daily_stop_hit": day_stopped,
        "day_type": day_type,
        "seed": args.seed,
    })

    print(f"day close | equity {final['equity']:.2f} | P&L {pnl:+.2f} "
          f"({pnl / float(start['equity']):+.2%}) | trades {ctx.trades_today} | "
          f"flat: {not open_pos}")
    if open_pos:
        print(f"WARNING: NOT FLAT AT CLOSE: {json.dumps(open_pos)}", file=sys.stderr)
    print(json.dumps(summary, indent=2))
    server.shutdown()
    return 0 if not open_pos else 1


if __name__ == "__main__":
    raise SystemExit(main())
