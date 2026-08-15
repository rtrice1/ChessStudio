"""Tests for the $10k realities: the PDT guard and the panic button."""
import json
import os
import tempfile
import unittest

from agent.metrics import day_trades_last_sessions
from agent.panic import flatten_book
from agent.risk import RiskLimits, check_order
from agent.strategist import Decision, DayPlan, SessionContext, execute


def account(equity, cash=None, positions=None):
    return {"accountId": "PAPER-001", "cash": cash if cash is not None else equity,
            "equity": equity, "positions": positions or []}


class TestPdtGuard(unittest.TestCase):
    def test_small_account_blocked_at_budget(self):
        v = check_order(account(10_000.0), "AAPL", "BUY", 5, 100.0,
                        day_trades_5d=3)
        self.assertFalse(v.approved)
        self.assertIn("PDT guard", v.reason)

    def test_small_account_allowed_under_budget(self):
        self.assertTrue(check_order(account(10_000.0), "AAPL", "BUY", 5, 100.0,
                                    day_trades_5d=2).approved)

    def test_sells_never_blocked_by_pdt(self):
        acct = account(10_000.0, positions=[{"symbol": "AAPL", "quantity": 5,
                                             "averagePrice": 100.0,
                                             "marketValue": 500.0}])
        self.assertTrue(check_order(acct, "AAPL", "SELL", 5, 100.0,
                                    day_trades_5d=99).approved)

    def test_large_account_exempt(self):
        self.assertTrue(check_order(account(30_000.0), "AAPL", "BUY", 5, 100.0,
                                    day_trades_5d=50).approved)

    def test_untracked_count_means_guard_off(self):
        # Sims pass None — the guard only binds when the runner tracks it.
        self.assertTrue(check_order(account(10_000.0), "AAPL", "BUY", 5, 100.0,
                                    day_trades_5d=None).approved)

    def test_limits_are_finra_numbers(self):
        limits = RiskLimits()
        self.assertEqual(limits.pdt_min_equity, 25_000.0)
        self.assertEqual(limits.max_day_trades_5d, 3)


class TestDayTradeCounting(unittest.TestCase):
    def write_ledger(self, tmp, entries):
        path = os.path.join(tmp, "ledger.jsonl")
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        return path

    def test_counts_sells_across_last_five_sessions(self):
        entries = []
        for day in range(7):  # 7 sessions, 1 round trip each
            entries.append({"kind": "session_start", "ts": f"d{day}"})
            entries.append({"kind": "fill", "action": "BUY", "symbol": "AAPL"})
            entries.append({"kind": "fill", "action": "SELL", "symbol": "AAPL"})
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_ledger(tmp, entries)
            # only the last 5 sessions count toward the rolling window
            self.assertEqual(day_trades_last_sessions(path, sessions=5), 5)

    def test_missing_ledger_counts_zero(self):
        self.assertEqual(day_trades_last_sessions("/nonexistent/l.jsonl"), 0)

    def test_execute_advances_the_rolling_count(self):
        class SellClient:
            def place_order(self, symbol, action, qty, order_type):
                return {"status": "FILLED", "fillPrice": 100.0}

            def account(self):
                return account(10_000.0)

            def quotes(self, symbols):
                return {s: {"bid": 100.0, "ask": 100.1} for s in symbols}

        class NullLedger:
            def record(self, *a, **k):
                pass

        pos = [{"symbol": "AAPL", "quantity": 5, "averagePrice": 99.0,
                "marketValue": 500.0}]
        snap = {"account": account(10_000.0, positions=pos),
                "quotes": {"AAPL": {"bid": 100.0, "ask": 100.1}}}
        ctx = SessionContext(day_open_equity=10_000.0, plan=DayPlan())
        ctx.day_trades_5d = 2
        execute([Decision("AAPL", "SELL", 5, "exit")], snap, ctx,
                SellClient(), NullLedger())
        self.assertEqual(ctx.day_trades_5d, 3)


class FakeBroker:
    def __init__(self, positions):
        self.positions = positions
        self.orders = []

    def account(self):
        return {"positions": self.positions}

    def place_order(self, symbol, action, qty, order_type):
        self.orders.append((symbol, action, qty))
        if symbol == "BROKEN":
            raise RuntimeError("no quote")
        return {"status": "FILLED", "fillPrice": 100.0}


class TestPanicButton(unittest.TestCase):
    def test_flattens_everything_and_survives_failures(self):
        broker = FakeBroker([
            {"symbol": "AAPL", "quantity": 5},
            {"symbol": "BROKEN", "quantity": 2},
            {"symbol": "MSFT", "quantity": 0},          # already flat
            {"symbol": "AAPL260821C00190000", "quantity": 1}])
        results = flatten_book(broker)
        attempted = {r["symbol"] for r in results}
        self.assertEqual(attempted, {"AAPL", "BROKEN", "AAPL260821C00190000"})
        # the broken symbol is recorded as failed, the rest still flattened
        by_sym = {r["symbol"]: r["order"].get("status") for r in results}
        self.assertEqual(by_sym["AAPL"], "FILLED")
        self.assertEqual(by_sym["BROKEN"], "ERROR")
        self.assertEqual(by_sym["AAPL260821C00190000"], "FILLED")


if __name__ == "__main__":
    unittest.main()
