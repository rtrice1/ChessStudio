"""Tests for the remaining legal-edge plumbing: board mention velocity,
halt alerts, slippage measurement, and momentum acceleration/inflection."""
import unittest

from agent.indicators import momentum_phase, summarize
from agent.metrics import slippage_stats
from agent.poller import compute_alerts, mention_velocity
from agent.strategist import (Decision, DayPlan, SessionContext, decide,
                              execute, score_entry)


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
        self.assertEqual(v, {"recent": 0, "prior": 0, "ratio": None,
                             "accel": None})

    def test_mention_acceleration_second_difference(self):
        # 1 post three hours back, 3 two hours back, 9 in the last hour:
        # accel = 9 - 2*3 + 1 = 4 (the swarm is still gathering speed)
        items = ([board_item("2026-08-19T11:30:00")]
                 + [board_item(f"2026-08-19T12:{m:02d}:00") for m in (10, 20, 30)]
                 + [board_item(f"2026-08-19T13:{m:02d}:00")
                    for m in range(5, 50, 5)])
        v = mention_velocity(items)
        self.assertEqual((v["recent"], v["prior"]), (9, 3))
        self.assertEqual(v["accel"], 4)


class TestHaltAlert(unittest.TestCase):
    def test_halted_quote_raises_alert(self):
        alerts = compute_alerts({}, {"AAPL": {"last": 100.0, "halted": True},
                                     "MSFT": {"last": 200.0}})
        kinds = [(a["symbol"], a["kind"]) for a in alerts]
        self.assertIn(("AAPL", "halted"), kinds)
        self.assertNotIn(("MSFT", "halted"), kinds)


def candles_with_increments(increments):
    """Build a rising price series whose per-bar gain is `increments`."""
    out, price = [], 100.0
    for i, inc in enumerate(increments):
        price += inc
        out.append({"datetime": f"2026-08-19T{9 + i // 60:02d}:{i % 60:02d}:00",
                    "open": price - inc, "high": price + 0.05,
                    "low": price - inc - 0.05, "close": price, "volume": 1000})
    return out


class TestAccelerationAndPhase(unittest.TestCase):
    def test_phase_quadrants(self):
        self.assertEqual(momentum_phase(1.0, 0.5), "accelerating")
        self.assertEqual(momentum_phase(1.0, -0.5), "exhausting")
        self.assertEqual(momentum_phase(-1.0, 0.5), "basing")
        self.assertEqual(momentum_phase(-1.0, -0.5), "falling")
        self.assertIsNone(momentum_phase(None, 0.5))
        self.assertIsNone(momentum_phase(1.0, None))

    def test_accelerating_series_classified(self):
        # gains grow every bar: velocity up, acceleration up
        candles = candles_with_increments([0.1 + 0.02 * i for i in range(60)])
        ind = summarize(candles)
        self.assertGreater(ind["roc_accel"], 0)
        self.assertEqual(ind["momentum_phase"], "accelerating")

    def test_exhausting_series_classified(self):
        # still rising every bar, but each gain smaller: the inflection
        # is forming while the chart alone still looks strong
        candles = candles_with_increments(
            [1.0] * 30 + [max(0.02, 1.0 - 0.15 * i) for i in range(30)])
        ind = summarize(candles)
        self.assertGreater(ind["roc10"], 0)
        self.assertLess(ind["roc_accel"], 0)
        self.assertEqual(ind["momentum_phase"], "exhausting")

    def test_score_penalizes_decelerating_breakout(self):
        base = {"rsi14": 55.0, "adx": 30.0, "macd_hist": 0.5}
        accel, _ = score_entry({**base, "momentum_phase": "accelerating",
                                "macd_hist_slope": 0.1})
        exhaust, why = score_entry({**base, "momentum_phase": "exhausting",
                                    "macd_hist_slope": -0.1})
        self.assertGreater(accel, exhaust)
        self.assertIn("inflection risk", why)


class TestInflectionExit(unittest.TestCase):
    IND = {"rsi14": 60.0, "atr14": 1.0, "vwap": 100.8,
           "range_high": 99.0, "range_low": 98.0,
           "momentum_phase": "exhausting", "macd_hist_slope": -0.05,
           "roc_accel": -0.4}

    def snap(self, ind):
        return {"account": {"equity": 100_000.0, "cash": 90_000.0,
                            "positions": [{"symbol": "AAPL", "quantity": 100,
                                           "averagePrice": 100.0}]},
                "quotes": {"AAPL": {"bid": 101.45, "ask": 101.55,
                                    "last": 101.5}},
                "indicators": {"AAPL": ind}}

    def ctx(self, **plan_kwargs):
        return SessionContext(day_open_equity=100_000.0,
                              plan=DayPlan(**plan_kwargs))

    def test_profitable_decelerating_position_sold_at_inflection(self):
        # +1.5 ATR in profit, below the 2.5 ATR target, momentum rolling
        decisions = decide(self.snap(self.IND), self.ctx())
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].action, "SELL")
        self.assertIn("inflection exit", decisions[0].rationale)

    def test_plan_can_disable_inflection_exits(self):
        decisions = decide(self.snap(self.IND),
                           self.ctx(take_inflection_exits=False))
        self.assertEqual(decisions, [])

    def test_accelerating_winner_keeps_riding(self):
        ind = {**self.IND, "momentum_phase": "accelerating",
               "macd_hist_slope": 0.05, "roc_accel": 0.4}
        self.assertEqual(decide(self.snap(ind), self.ctx()), [])

    def test_insufficient_profit_waits(self):
        # exhausting but only +0.5 ATR in profit: not worth paying the
        # spread to dodge a maybe-inflection; the stop is the guard
        snap = self.snap(self.IND)
        snap["account"]["positions"][0]["averagePrice"] = 101.0
        self.assertEqual(decide(snap, self.ctx()), [])


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
