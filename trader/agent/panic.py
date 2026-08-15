"""The panic button: halt everything, then flatten everything.

    python -m agent.panic              # real-data shadow book (Monday's mode)
    python -m agent.panic --mock       # against the local mock broker

Software stops only work while the software runs. This is the manual
backstop for the human (or a watchdog) when the loop is dead, wedged, or
simply not trusted right now:

1. touch data/HALT — the risk gate refuses every new order desk-wide,
   including from a loop that's still half-alive.
2. market-sell every open position in the book, options included.

Order of operations matters: halt FIRST, so nothing re-enters while the
flatten is in flight. The HALT file stays behind afterwards — removing
it is a human decision, like everything else about restarting.

In shadow mode this closes the local book at real quotes. If real order
routing is ever armed (a human-flipped gate that does not exist today),
this same tool is the emergency exit, which is why it exists and is
tested now rather than being written during the first real emergency.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.ledger import Ledger                 # noqa: E402

SYMBOLS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN",
           "SPY", "QQQ", "TSLA", "JPM", "XOM"]


def flatten_book(broker, ledger=None) -> list[dict]:
    """Market-sell every open position. Returns the attempted orders.
    Failures are recorded and skipped — one dead symbol must not strand
    the rest of the book."""
    results = []
    for p in broker.account().get("positions", []):
        qty = int(p.get("quantity", 0))
        if qty <= 0:
            continue
        symbol = p["symbol"]
        try:
            order = broker.place_order(symbol, "SELL", qty, "MARKET")
        except Exception as exc:  # keep going — flatten what can be flattened
            order = {"status": "ERROR", "error": str(exc)}
        entry = {"symbol": symbol, "quantity": qty, "order": order}
        results.append(entry)
        if ledger is not None:
            ledger.record("panic_flatten", entry)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--data-dir", default=os.path.join(base, "data"))
    ap.add_argument("--mock", action="store_true",
                    help="flatten against the local mock broker instead")
    ap.add_argument("--base-url", default="http://127.0.0.1:8788")
    args = ap.parse_args()

    # Halt FIRST: from this moment no order passes the risk gate.
    os.makedirs(args.data_dir, exist_ok=True)
    halt = os.path.join(args.data_dir, "HALT")
    open(halt, "w").close()
    print(f"HALT engaged: {halt}")

    if args.mock:
        from agent.client import BrokerClient
        broker = BrokerClient(args.base_url)
    else:
        from agent.schwab import SchwabClient, TokenStore
        from agent.shadow import ShadowBroker
        broker = ShadowBroker(
            SchwabClient(TokenStore()), SYMBOLS,
            book_path=os.path.join(args.data_dir, "shadow_book.json"))

    ledger = Ledger(os.path.join(args.data_dir, "ledger.jsonl"))
    results = flatten_book(broker, ledger)
    if hasattr(broker, "save"):
        broker.save()
    if not results:
        print("book already flat")
    for r in results:
        status = r["order"].get("status", "?")
        print(f"SELL {r['quantity']}x {r['symbol']}: {status}")
    failed = [r for r in results if r["order"].get("status") != "FILLED"]
    print(f"flattened {len(results) - len(failed)}/{len(results)} positions"
          + (f" — {len(failed)} FAILED, intervene manually" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
