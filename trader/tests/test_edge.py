"""Tests for the remaining legal-edge plumbing: board mention velocity,
halt alerts, and slippage measurement."""
import unittest

from agent.metrics import slippage_stats
from agent.poller import compute_alerts, mention_velocity
from agent.strategist import Decision, DayPlan, SessionContext, execute


def board_item(ts):
    return {"ts": ts, "source": "board", "headline": "x"}


class TestMentionVelocity(unittest.TestCase):
    def test_acceleration_measured_against_prior_hour(self):
        items = ([board_item(f"2026-08-19T14:{m:02d}:00") for m in range(0, 60, 10)]
                 + [board_item("2026-08-19T13:30:00")])
        v = mention_velocity(items)
        self.assertEqual(v["recent"], 6)
        self.assertEqual(v["prior"], 1)
        self.assertEqual(v["ratio"], 6.0)

    def test_no_prior_hour_means_no_ratio(self):
        v = mention_velocity([board_item("2026-08-19T14:00:00")])
        self.assertIsNone(v["ratio"])

    def test_wire_items_do_not_count(self):
        items = [{"ts": "2026-08-19T14:00:00", "source": "wire",
                  "headline": "x"}]
        self.assertEqual(mention_velocity(items)["recent"], 0)

    def test_bad_timestamps_skipped(self):
        v = mention_velocity([board_item("garbage"), board_item("")])
        self.assertEqual(v, {"recent": 0, "prior": 0, "ratio": None})


class TestHaltAlert(unittest.TestCase):
    def test_halted_quote_raises_alert(self):
        alerts = compute_alerts({}, {"AAPL": {"last": 100.0, "halted": True},
                                     "MSFT": {"last": 200.0}})
        kinds = [(a["symbol"], a["kind"]) for a in alerts]
        self.assertIn(("AAPL", "halted"), kinds)
        self.assertNotIn(("MSFT", "halted"), kinds)


class RecordingLedger:
    def __init__(self):
        self.records = []

    def record(self, kind, payload):
        self.records.append({"kind": kind, **payload})


class FillClient:
    """Fills every market order at the ask (BUY) / bid (SELL)."""
    def __init__(self, bid, ask):
        self.bid, self.ask = bid, ask

    def place_order(self, symbol, action, qty, order_type):
        price = self.ask if action == "BUY" else self.bid
        return {"status": "FILLED", "fillPrice": price}

    def account(self):
        return {"equity": 100_000.0, "cash": 100_000.0, "positions": []}

    def quotes(self, symbols):
        return {s: {"bid": self.bid, "ask": self.ask} for s in symbols}


class TestSlippageMeasurement(unittest.TestCase):
    def test_fill_records_cost_vs_mid(self):
        ledger = RecordingLedger()
        snap = {"account": {"equity": 100_000.0, "cash": 100_000.0,
                            "positions": []},
                "quotes": {"AAPL": {"bid": 100.0, "ask": 100.10,
                                    "last": 100.05}}}
        ctx = SessionContext(day_open_equity=100_000.0, plan=DayPlan())
        execute([Decision("AAPL", "BUY", 10, "test")], snap, ctx,
                FillClient(100.0, 100.10), ledger)
        fills = [r for r in ledger.records if r["kind"] == "fill"]
        self.assertEqual(len(fills), 1)
        slip = fills[0]["slippage"]
        # filled at 100.10 vs mid 100.05 -> half the spread, per share
        self.assertAlmostEqual(slip["vs_mid_per_share"], 0.05)
        self.assertAlmostEqual(slip["vs_mid_total"], 0.50)
        self.assertAlmostEqual(slip["half_spread"], 0.05)

    def test_stats_aggregate_and_ignore_unmeasured(self):
        fills = [{"slippage": {"vs_mid_total": 0.50, "half_spread": 0.05}},
                 {"slippage": {"vs_mid_total": 1.50, "half_spread": 0.10}},
                 {"slippage": None},        # pre-measurement fill
                 {}]                         # ancient fill
        stats = slippage_stats(fills)
        self.assertEqual(stats["measured_fills"], 2)
        self.assertAlmostEqual(stats["total_vs_mid"], 2.0)
        self.assertAlmostEqual(stats["avg_per_fill"], 1.0)

    def test_no_measured_fills(self):
        self.assertEqual(slippage_stats([]), {"measured_fills": 0})


if __name__ == "__main__":
    unittest.main()
