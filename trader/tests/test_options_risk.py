"""Tests for options risk rules and the calls-mode strategist plumbing."""
import unittest

from agent.risk import check_order, is_option
from agent.strategist import DayPlan, Decision, option_exits, pick_call, translate_to_calls

OCC = "AAPL260821C00190000"


def account(cash=100_000.0, equity=100_000.0, positions=None):
    return {"cash": cash, "equity": equity, "positions": positions or []}


class TestOptionDetection(unittest.TestCase):
    def test_occ_symbols_detected(self):
        self.assertTrue(is_option(OCC))
        self.assertTrue(is_option("SPY260814P00450000"))
        self.assertFalse(is_option("AAPL"))
        self.assertFalse(is_option(""))


class TestOptionRisk(unittest.TestCase):
    def test_premium_per_order_cap(self):
        # 10 contracts x $250 = $2500 > 2% of 100k
        v = check_order(account(), OCC, "BUY", 10, 250.0)
        self.assertFalse(v.approved)
        self.assertIn("premium", v.reason)
        self.assertTrue(check_order(account(), OCC, "BUY", 7, 250.0).approved)

    def test_total_premium_cap(self):
        held = [{"symbol": "SPY260814C00450000", "quantity": 10,
                 "averagePrice": 500.0, "marketValue": 5000.0}]
        # deployed 5000 + new 1800 = 6800 > 6% of 100k
        v = check_order(account(positions=held), OCC, "BUY", 9, 200.0)
        self.assertFalse(v.approved)
        self.assertIn("total option premium", v.reason)

    def test_option_sells_use_held_quantity_rule(self):
        held = [{"symbol": OCC, "quantity": 5, "averagePrice": 200.0}]
        self.assertTrue(check_order(account(positions=held), OCC, "SELL",
                                    5, 180.0).approved)
        self.assertFalse(check_order(account(positions=held), OCC, "SELL",
                                     6, 180.0).approved)

    def test_option_buys_respect_daily_caps(self):
        v = check_order(account(), OCC, "BUY", 1, 100.0, trades_today=40)
        self.assertFalse(v.approved)
        v = check_order(account(), OCC, "BUY", 1, 100.0, session_pct=0.95)
        self.assertFalse(v.approved)


class FakeClient:
    def __init__(self, chain=None, quotes=None):
        self._chain = chain or {}
        self._quotes = quotes or {}

    def chain(self, symbol, expiry=None):
        return self._chain

    def quotes(self, symbols):
        return {s: self._quotes.get(s, {}) for s in symbols}


CHAIN = {"symbol": "AAPL", "expiry": "2026-08-21", "calls": [
    {"contractSymbol": "AAPL260821C00185000", "strike": 185.0, "delta": 0.75,
     "bid": 6.0, "ask": 6.4},
    {"contractSymbol": "AAPL260821C00190000", "strike": 190.0, "delta": 0.52,
     "bid": 3.0, "ask": 3.2},
    {"contractSymbol": "AAPL260821C00195000", "strike": 195.0, "delta": 0.30,
     "bid": 1.2, "ask": 1.4},
]}


class TestCallsTranslation(unittest.TestCase):
    def test_picks_near_the_money_by_delta(self):
        self.assertEqual(pick_call(CHAIN)["strike"], 190.0)

    def test_translate_sizes_by_premium_budget(self):
        d = Decision("AAPL", "BUY", 98, "ORB: breakout")
        out = translate_to_calls(d, FakeClient(chain=CHAIN), 100_000.0, DayPlan())
        # budget 1% of 100k = $1000; contract cost = 3.2*100 = $320 -> 3 contracts
        self.assertEqual(out.symbol, "AAPL260821C00190000")
        self.assertEqual(out.quantity, 3)
        self.assertIn("as calls", out.rationale)

    def test_translate_none_when_budget_below_one_contract(self):
        d = Decision("AAPL", "BUY", 98, "ORB")
        out = translate_to_calls(d, FakeClient(chain=CHAIN), 20_000.0, DayPlan())
        self.assertIsNone(out)  # $200 budget < $320 contract

    def test_translate_none_on_chain_error(self):
        d = Decision("AAPL", "BUY", 98, "ORB")
        self.assertIsNone(translate_to_calls(
            d, FakeClient(chain={"error": "x"}), 100_000.0, DayPlan()))


class TestOptionExits(unittest.TestCase):
    def positions(self, avg=300.0):
        return {"positions": [{"symbol": OCC, "quantity": 3,
                               "averagePrice": avg}]}

    def test_premium_stop(self):
        client = FakeClient(quotes={OCC: {"bid": 140.0, "ask": 150.0}})
        exits = option_exits(self.positions(avg=300.0), client, DayPlan())
        self.assertEqual(len(exits), 1)
        self.assertIn("premium stop", exits[0].rationale)

    def test_premium_target(self):
        client = FakeClient(quotes={OCC: {"bid": 620.0, "ask": 640.0}})
        exits = option_exits(self.positions(avg=300.0), client, DayPlan())
        self.assertEqual(len(exits), 1)
        self.assertIn("premium target", exits[0].rationale)

    def test_no_exit_in_between(self):
        client = FakeClient(quotes={OCC: {"bid": 280.0, "ask": 300.0}})
        self.assertEqual(option_exits(self.positions(avg=300.0), client,
                                      DayPlan()), [])

    def test_stock_positions_ignored(self):
        client = FakeClient()
        acct = {"positions": [{"symbol": "AAPL", "quantity": 100,
                               "averagePrice": 100.0}]}
        self.assertEqual(option_exits(acct, client, DayPlan()), [])


if __name__ == "__main__":
    unittest.main()
