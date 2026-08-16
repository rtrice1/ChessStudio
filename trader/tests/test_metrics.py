"""Tests for the desk scoreboard."""
import unittest

from agent.metrics import (daily_stats, hunch_calibration, judgment_cost,
                           reject_histogram, round_trips, trade_stats)


def fill(symbol, action, qty, price):
    return {"kind": "fill", "symbol": symbol, "action": action,
            "quantity": qty, "order": {"fillPrice": price}}


class TestRoundTrips(unittest.TestCase):
    def test_simple_round_trip(self):
        trips = round_trips([fill("AAPL", "BUY", 100, 100.0),
                             fill("AAPL", "SELL", 100, 101.0)])
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["pnl"], 100.0)

    def test_fifo_partial_fills(self):
        trips = round_trips([fill("AAPL", "BUY", 100, 100.0),
                             fill("AAPL", "BUY", 50, 102.0),
                             fill("AAPL", "SELL", 120, 103.0)])
        # 100 @ 100 -> +300, then 20 of the 102 lot -> +20
        self.assertEqual([t["pnl"] for t in trips], [300.0, 20.0])
        self.assertEqual([t["quantity"] for t in trips], [100, 20])

    def test_symbols_do_not_cross(self):
        trips = round_trips([fill("AAPL", "BUY", 10, 100.0),
                             fill("MSFT", "SELL", 10, 100.0),
                             fill("AAPL", "SELL", 10, 99.0)])
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["symbol"], "AAPL")
        self.assertEqual(trips[0]["pnl"], -10.0)


class TestReasoningStats(unittest.TestCase):
    def rfill(self, symbol, action, qty, price, rationale):
        return {**fill(symbol, action, qty, price), "rationale": rationale}

    def test_rationales_ride_the_round_trip(self):
        from agent.metrics import round_trips
        trips = round_trips([
            self.rfill("AAPL", "BUY", 10, 100.0, "ORB: breakout | score +3.50 (adx)"),
            self.rfill("AAPL", "SELL", 10, 103.0, "ATR target: 103 >= 102.5")])
        self.assertEqual(trips[0]["entry_rationale"],
                         "ORB: breakout | score +3.50 (adx)")
        self.assertEqual(trips[0]["exit_rationale"], "ATR target: 103 >= 102.5")

    def test_pnl_grouped_by_exit_reason_and_entry_score(self):
        from agent.metrics import reasoning_stats, round_trips
        fills = []
        # two strong-score winners via ATR target, one weak-score loser via stop
        for px_out, score in ((103.0, "+3.50"), (103.0, "+3.25")):
            fills.append(self.rfill("AAPL", "BUY", 10, 100.0,
                                    f"ORB: x | score {score} (stuff)"))
            fills.append(self.rfill("AAPL", "SELL", 10, px_out,
                                    "ATR target: hit"))
        fills.append(self.rfill("MSFT", "BUY", 10, 100.0,
                                "ORB: x | score +0.25 (thin)"))
        fills.append(self.rfill("MSFT", "SELL", 10, 98.0, "ATR stop: hit"))
        r = reasoning_stats(round_trips(fills))
        self.assertEqual(r["by_exit"]["atr target"]["n"], 2)
        self.assertEqual(r["by_exit"]["atr target"]["win_rate"], 1.0)
        self.assertEqual(r["by_exit"]["atr stop"]["total_pnl"], -20.0)
        self.assertEqual(r["by_entry_score"][">=3"]["n"], 2)
        self.assertEqual(r["by_entry_score"]["0-1"]["win_rate"], 0.0)

    def test_unscored_entries_are_not_binned(self):
        from agent.metrics import reasoning_stats, round_trips
        fills = [self.rfill("AAPL", "BUY", 10, 100.0, "manual entry, no score"),
                 self.rfill("AAPL", "SELL", 10, 101.0, "flatten: end of day")]
        r = reasoning_stats(round_trips(fills))
        self.assertEqual(r["by_entry_score"], {})
        self.assertIn("flatten", r["by_exit"])

    def test_plan_stats_separates_benchmark_llm_and_shaded(self):
        from agent.metrics import plan_stats
        journal = [
            {"kind": "trading_day", "pnl_pct": 0.001,
             "plan": "default mechanical plan (benchmark)"},
            {"kind": "trading_day", "pnl_pct": -0.002,
             "plan": "default mechanical plan (benchmark) | gut: chop suspected, risk halved"},
            {"kind": "trading_day", "pnl_pct": 0.004,
             "plan": "NVDA momentum day, 3 slots, calls off"},
            {"kind": "note", "text": "not a trading day"},
        ]
        p = plan_stats(journal)
        self.assertEqual(p["benchmark"]["days"], 2)
        self.assertEqual(p["llm_plan"]["days"], 1)
        self.assertEqual(p["gut_shaded"]["days"], 1)
        self.assertEqual(p["llm_plan"]["avg_pnl_pct"], 0.004)


class TestTradeStats(unittest.TestCase):
    def test_expectancy_and_profit_factor(self):
        trips = [{"pnl": 100.0}, {"pnl": 100.0}, {"pnl": -50.0}, {"pnl": -50.0}]
        stats = trade_stats(trips)
        self.assertEqual(stats["win_rate"], 0.5)
        self.assertEqual(stats["expectancy_per_trade"], 25.0)  # .5*100 - .5*50
        self.assertEqual(stats["profit_factor"], 2.0)
        self.assertEqual(stats["net_pnl"], 100.0)

    def test_all_winners_has_no_profit_factor(self):
        stats = trade_stats([{"pnl": 10.0}])
        self.assertIsNone(stats["profit_factor"])

    def test_empty(self):
        self.assertEqual(trade_stats([]), {"trades": 0})


class TestDailyStats(unittest.TestCase):
    def days(self, rets, flat=True):
        return [{"kind": "trading_day", "pnl_pct": r, "flat_at_close": flat}
                for r in rets]

    def test_drawdown_and_flat_rate(self):
        stats = daily_stats(self.days([0.01, -0.02, 0.01]))
        self.assertEqual(stats["days"], 3)
        self.assertEqual(stats["flat_at_close_rate"], 1.0)
        self.assertAlmostEqual(stats["max_drawdown"], 0.02, places=6)
        self.assertEqual(stats["worst_day"], -0.02)

    def test_not_flat_shows_up(self):
        days = self.days([0.01]) + self.days([0.01], flat=False)
        self.assertEqual(daily_stats(days)["flat_at_close_rate"], 0.5)


class TestHunchCalibration(unittest.TestCase):
    def test_grades_by_date_and_reports_gap(self):
        ledger = [
            {"kind": "gut_check", "ts": "2026-08-14T15:00:00+00:00",
             "hunch": {"suspected_day_type": "chop", "confidence": 0.9}},
            {"kind": "gut_check", "ts": "2026-08-15T15:00:00+00:00",
             "hunch": {"suspected_day_type": "trend_up", "confidence": 0.9}},
        ]
        journal = [
            {"kind": "trading_day", "ts": "2026-08-14T20:10:00+00:00",
             "day_type": "chop"},
            {"kind": "trading_day", "ts": "2026-08-15T20:10:00+00:00",
             "day_type": "chop"},
        ]
        cal = hunch_calibration(ledger, journal)
        self.assertEqual(cal["graded"], 2)
        self.assertEqual(cal["accuracy"], 0.5)
        # confidence 0.9 vs accuracy 0.5 -> overconfident by 0.4
        self.assertAlmostEqual(cal["calibration_gap"], 0.4, places=4)
        self.assertIn("trend_up->chop", cal["confusion"])

    def test_no_data(self):
        self.assertEqual(hunch_calibration([], []), {"graded": 0})


class TestOpsMetrics(unittest.TestCase):
    def test_reject_histogram_groups_reasons(self):
        ledger = [{"kind": "risk_reject", "reason": "daily trade cap: 40 >= 40"},
                  {"kind": "risk_reject", "reason": "daily trade cap: 41 >= 40"},
                  {"kind": "risk_reject", "reason": "entry cutoff: 92% of session"}]
        hist = reject_histogram(ledger)
        self.assertEqual(hist["daily trade cap:"], 2)
        self.assertEqual(hist["entry cutoff: 92%"], 1)

    def test_judgment_cost_counts_denials(self):
        tokens = [{"status": "dry_run", "kind": "day_plan", "est_cost_usd": 0.05},
                  {"status": "ok", "kind": "post_mortem", "est_cost_usd": 0.06},
                  {"status": "budget_denied", "kind": "day_plan", "est_cost_usd": 0.0}]
        cost = judgment_cost(tokens)
        self.assertEqual(cost["invocations"], 2)
        self.assertEqual(cost["refusals_and_denials"], 1)
        self.assertAlmostEqual(cost["est_cost_usd"], 0.11, places=4)


if __name__ == "__main__":
    unittest.main()
