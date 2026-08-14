"""Run an end-to-end accelerated paper-trading session against the mock API.

Starts the mock Schwab server in-process, then loops: poll -> decide -> risk
gate -> execute -> log, at an accelerated sim clock. This is the smoke-test
harness; in real deployment the poller, strategist, and server are separate
processes on separate schedules (see SPEC/AGENTS.md).

Usage:
    cd trader && python -m agent.run_paper --cycles 20 --time-scale 300
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
from agent.ledger import Ledger                 # noqa: E402
from agent.poller import poll_once              # noqa: E402
from agent.strategist import SessionContext, decide, execute  # noqa: E402
from mockschwab.server import create_server     # noqa: E402

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
                   "SPY", "QQQ", "TSLA", "JPM", "XOM"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cycles", type=int, default=20, help="poll/decide cycles to run")
    ap.add_argument("--cycle-seconds", type=float, default=1.0,
                    help="real seconds between cycles")
    ap.add_argument("--time-scale", type=float, default=300.0,
                    help="sim seconds per real second (300 = 5 sim min per real sec)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--starting-cash", type=float, default=100_000.0)
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "..", "data"))
    args = ap.parse_args()

    server = create_server(port=args.port, seed=args.seed,
                           time_scale=args.time_scale,
                           starting_cash=args.starting_cash)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    base_url = f"http://127.0.0.1:{args.port}"
    client = BrokerClient(base_url)
    ledger = Ledger(os.path.join(args.data_dir, "ledger.jsonl"))

    start = client.account()
    ctx = SessionContext(day_open_equity=float(start["equity"]))
    ledger.record("session_start", {"equity": start["equity"], "seed": args.seed,
                                    "time_scale": args.time_scale})
    print(f"session start | equity {start['equity']:.2f} | "
          f"time_scale {args.time_scale}x | {args.cycles} cycles")

    for cycle in range(1, args.cycles + 1):
        try:
            snapshot = poll_once(client, DEFAULT_SYMBOLS, args.data_dir)
            decisions = decide(snapshot, ctx)
            executed = execute(decisions, snapshot, ctx, client, ledger)
            account = client.account()
            npos = len([p for p in account["positions"] if p["quantity"]])
            print(f"cycle {cycle:3d} | equity {account['equity']:>10.2f} | "
                  f"cash {account['cash']:>10.2f} | positions {npos} | "
                  f"decisions {len(decisions)} | executed {len(executed)} | "
                  f"alerts {len(snapshot.get('alerts', []))}")
        except Exception as exc:  # keep the session alive through single-cycle failures
            print(f"cycle {cycle:3d} | ERROR: {exc}", file=sys.stderr)
        time.sleep(args.cycle_seconds)

    final = client.account()
    pnl = float(final["equity"]) - float(start["equity"])
    ledger.record("session_end", {"equity": final["equity"], "pnl": pnl,
                                  "ledger_summary": ledger.summary()})
    print(f"session end   | equity {final['equity']:.2f} | "
          f"P&L {pnl:+.2f} ({pnl / float(start['equity']):+.2%})")
    print(json.dumps(ledger.summary(), indent=2))
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
