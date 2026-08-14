"""Tests for the day-trading strategist and the day-trading risk rules."""
import unittest

from agent.risk import RiskLimits, check_order
from agent.strategist import DayPlan, SessionContext, decide, flatten_all


def account(cash=100_000.0, equity=100_000.0, positions=None):
    return {"accountId": "PAPER-001", "cash": cash, "equity": equity,
            "positions": positions or []}


def snapshot(acct, quotes, indicators):
    return {"account": acct, "quotes": quotes, "indicators": indicators}


IND_BREAKOUT = {"rsi14": 55.0, "sma20": 100.0, "atr14": 1.0, "vwap": 100.5,
                "range_high": 101.0, "range_low": 99.5}
QUOTE = {"symbol": "AAPL", "bid": 101.45, "ask": 101.55, "last": 101.5}


class TestDayRiskRules(unittest.TestCase):
    def test_daily_trade_cap_blocks_buys(self):
        v = check_order(account(), "AAPL", "BUY", 10, 100.0, trades_today=40)
        self.assertFalse(v.approved)
        self.assertIn("daily trade cap", v.reason)

    def test_under_trade_cap_allows(self):
        self.assertTrue(check_order(account(), "AAPL", "BUY", 10, 100.0,
                                    trades_today=39).approved)

    def test_late_session_entry_cutoff(self):
        v = check_order(account(), "AAPL", "BUY", 10, 100.0, session_pct=0.95)
        self.assertFalse(v.approved)
        self.assertIn("entry cutoff", v.reason)

    def test_sells_exempt_from_day_caps(self):
        acct = account(positions=[{"symbol": "AAPL", "quantity": 10,
                                   "averagePrice": 100.0, "marketValue": 1000.0}])
        v = check_order(acct, "AAPL", "SELL", 10, 100.0,
                        trades_today=40, session_pct=0.99)
        self.assertTrue(v.approved)

    def test_limits_defaults(self):
        limits = RiskLimits()
        self.assertEqual(limits.max_daily_trades, 40)
        self.assertLess(limits.entry_cutoff_session_pct, 1.0)


class TestDayStrategist(unittest.TestCase):
    def ctx(self, **plan_kwargs):
        return SessionContext(day_open_equity=100_000.0,
                              plan=DayPlan(**plan_kwargs))

    def test_orb_breakout_buys_with_risk_sizing(self):
        snap = snapshot(account(), {"AAPL": QUOTE}, {"AAPL": IND_BREAKOUT})
        decisions = decide(snap, self.ctx())
        self.assertEqual(len(decisions), 1)
        d = decisions[0]
        self.assertEqual((d.symbol, d.action), ("AAPL", "BUY"))
        # stop = max(range_low, last - 1.5*atr) = max(99.5, 100.0) = 100.0
        # risk-based qty = (0.005 * 100000) // (101.5 - 100.0) = 333, but the
        # notional cap min(15%, 10%) * equity = 10000 limits it to 98 shares.
        self.assertEqual(d.quantity, 98)

    def test_tight_stop_does_not_produce_uncapped_size(self):
        ind = {**IND_BREAKOUT, "atr14": 0.02, "range_low": 101.4}
        snap = snapshot(account(), {"AAPL": QUOTE}, {"AAPL": ind})
        decisions = decide(snap, self.ctx())
        self.assertEqual(len(decisions), 1)
        # risk-based size would be 500 // 0.03 = 16666 shares (~$1.7M);
        # the cap keeps notional within 10% of equity.
        self.assertLessEqual(decisions[0].quantity * QUOTE["last"], 10_000.0)

    def test_no_entry_below_vwap(self):
        ind = {**IND_BREAKOUT, "vwap": 102.0}  # last 101.5 < vwap
        snap = snapshot(account(), {"AAPL": QUOTE}, {"AAPL": ind})
        self.assertEqual(decide(snap, self.ctx()), [])

    def test_no_entry_inside_range(self):
        ind = {**IND_BREAKOUT, "range_high": 102.5}
        snap = snapshot(account(), {"AAPL": QUOTE}, {"AAPL": ind})
        self.assertEqual(decide(snap, self.ctx()), [])

    def test_plan_symbol_filter_and_bias_off(self):
        snap = snapshot(account(), {"AAPL": QUOTE}, {"AAPL": IND_BREAKOUT})
        self.assertEqual(decide(snap, self.ctx(symbols=["MSFT"])), [])
        self.assertEqual(decide(snap, self.ctx(bias={"AAPL": "off"})), [])

    def test_atr_stop_exit(self):
        pos = [{"symbol": "AAPL", "quantity": 100, "averagePrice": 104.0,
                "marketValue": 10150.0}]
        # stop = 104 - 1.5*1.0 = 102.5; last 101.5 <= stop -> SELL
        snap = snapshot(account(positions=pos), {"AAPL": QUOTE},
                        {"AAPL": IND_BREAKOUT})
        decisions = decide(snap, self.ctx())
        self.assertEqual(len(decisions), 1)
        self.assertEqual((decisions[0].action, decisions[0].quantity), ("SELL", 100))
        self.assertIn("stop", decisions[0].rationale)

    def test_atr_target_exit(self):
        pos = [{"symbol": "AAPL", "quantity": 100, "averagePrice": 98.0,
                "marketValue": 10150.0}]
        # target = 98 + 2.5*1.0 = 100.5; last 101.5 >= target -> SELL
        snap = snapshot(account(positions=pos), {"AAPL": QUOTE},
                        {"AAPL": IND_BREAKOUT})
        decisions = decide(snap, self.ctx())
        self.assertEqual(len(decisions), 1)
        self.assertIn("target", decisions[0].rationale)

    def test_vwap_fail_exit(self):
        pos = [{"symbol": "AAPL", "quantity": 100, "averagePrice": 101.4,
                "marketValue": 10150.0}]
        ind = {**IND_BREAKOUT, "vwap": 103.0}  # last well below vwap, inside stop/target
        snap = snapshot(account(positions=pos), {"AAPL": QUOTE}, {"AAPL": ind})
        decisions = decide(snap, self.ctx())
        self.assertEqual(len(decisions), 1)
        self.assertIn("VWAP", decisions[0].rationale)

    def test_holding_position_blocks_reentry_not_exit_logic(self):
        # held and between stop/target/vwap thresholds -> no decision at all
        pos = [{"symbol": "AAPL", "quantity": 100, "averagePrice": 101.0,
                "marketValue": 10150.0}]
        snap = snapshot(account(positions=pos), {"AAPL": QUOTE},
                        {"AAPL": IND_BREAKOUT})
        self.assertEqual(decide(snap, self.ctx()), [])

    def test_flatten_all(self):
        acct = account(positions=[
            {"symbol": "AAPL", "quantity": 100, "averagePrice": 100.0},
            {"symbol": "MSFT", "quantity": 0, "averagePrice": 0.0},
            {"symbol": "NVDA", "quantity": 7, "averagePrice": 50.0}])
        decisions = flatten_all(acct, "end of day")
        self.assertEqual({(d.symbol, d.action, d.quantity) for d in decisions},
                         {("AAPL", "SELL", 100), ("NVDA", "SELL", 7)})
        for d in decisions:
            self.assertIn("flatten", d.rationale)


if __name__ == "__main__":
    unittest.main()
