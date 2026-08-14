"""Tests for the focus subsystem."""
import unittest

from agent.focus import (FocusState, Item, assess, build_context,
                         items_from_snapshot, salience)


def account(equity=100_000.0, positions=None):
    return {"equity": equity, "cash": equity, "positions": positions or []}


POS_NVDA = {"symbol": "NVDA", "quantity": 50, "averagePrice": 180.0,
            "marketValue": 9000.0, "unrealizedPnl": -50.0}


class TestAssess(unittest.TestCase):
    def test_quiet_and_flat_is_wide(self):
        state = assess(account(), [], None, 0.3, 100_000.0)
        self.assertLess(state.width, 0.3)
        self.assertEqual(state.topics, [])

    def test_drawdown_locks_focus(self):
        state = assess(account(equity=98_000.0, positions=[POS_NVDA]),
                       [], None, 0.5, 100_000.0)
        self.assertEqual(state.width, 1.0)
        self.assertIn("NVDA", state.topics)

    def test_alerts_on_held_symbols_narrow(self):
        state = assess(account(positions=[POS_NVDA]),
                       [{"symbol": "NVDA", "kind": "big_move"}],
                       None, 0.5, 100_000.0)
        self.assertGreaterEqual(state.width, 0.9)
        self.assertEqual(state.topics, ["NVDA"])

    def test_late_session_with_positions(self):
        state = assess(account(positions=[POS_NVDA]), [], None, 0.9, 100_000.0)
        self.assertGreaterEqual(state.width, 0.8)
        self.assertIn("flat", state.reason)


class TestSalience(unittest.TestCase):
    def test_narrow_focus_amplifies_on_topic(self):
        narrow = FocusState(1.0, ["NVDA"])
        on = Item("a", "x", 0.5, topics=["NVDA"])
        off = Item("b", "x", 0.5, topics=["XOM"])
        self.assertGreater(salience(on, narrow), salience(off, narrow) * 5)

    def test_wide_focus_treats_topics_evenly(self):
        wide = FocusState(0.0, ["NVDA"])
        on = Item("a", "x", 0.5, topics=["NVDA"])
        off = Item("b", "x", 0.5, topics=["XOM"])
        self.assertAlmostEqual(salience(on, wide), salience(off, wide))

    def test_topicless_items_ride_priority(self):
        narrow = FocusState(1.0, ["NVDA"])
        risk = Item("risk", "x", 1.0)
        news = Item("news", "x", 0.2, topics=["NVDA"])
        self.assertGreater(salience(risk, narrow), 0.4)
        self.assertGreater(salience(risk, narrow), salience(news, narrow) * 0.8)


class TestBuildContext(unittest.TestCase):
    def items(self):
        return [
            Item("risk:account", "R" * 50, 1.0),
            Item("position:NVDA", "P" * 50, 0.9, topics=["NVDA"]),
            Item("alert:NVDA:0", "A" * 50, 0.7, topics=["NVDA"]),
            Item("market:NVDA", "M" * 50, 0.3, topics=["NVDA"]),
            Item("market:XOM", "X" * 50, 0.3, topics=["XOM"]),
            Item("news:XOM", "N" * 400, 0.2, topics=["XOM"]),
        ]

    def test_budget_is_respected_and_ranked(self):
        result = build_context(self.items(), FocusState(0.9, ["NVDA"]),
                               budget_chars=220)
        self.assertLessEqual(len(result["text"]), 220)
        self.assertIn("risk:account", result["included"])
        self.assertIn("position:NVDA", result["included"])
        self.assertIn("news:XOM", result["excluded"])

    def test_narrow_prefers_topic_over_offtopic(self):
        result = build_context(self.items(), FocusState(0.9, ["NVDA"]),
                               budget_chars=270)
        self.assertIn("market:NVDA", result["included"])
        self.assertNotIn("market:XOM", result["included"])

    def test_exclusions_are_visible(self):
        result = build_context(self.items(), FocusState(0.9, ["NVDA"]),
                               budget_chars=220)
        self.assertEqual(set(result["included"]) | set(result["excluded"]),
                         {it.key for it in self.items()})

    def test_refocus_converges_on_alerted_topic(self):
        # start with wrong topics; alert item pulls focus to NVDA on pass 2
        result = build_context(self.items(), FocusState(0.6, ["XOM"]),
                               budget_chars=300)
        self.assertGreaterEqual(result["passes"], 2)
        self.assertIn("NVDA", result["state"].topics)


class TestItemsFromSnapshot(unittest.TestCase):
    def test_snapshot_becomes_items(self):
        snapshot = {
            "account": account(positions=[POS_NVDA]),
            "quotes": {"NVDA": {"last": 178.0}},
            "indicators": {"NVDA": {"rsi14": 40.0, "vwap": 179.0,
                                    "range_low": 177.0, "range_high": 181.0}},
            "alerts": [{"symbol": "NVDA", "kind": "big_move", "detail": "-3.2%"}],
            "news": {"summary": {"NVDA": {"count": 5, "wire_sentiment": 1,
                                          "board_sentiment": -2,
                                          "latest_headline": "NVDA is cooked"}}},
        }
        desk = {"identity": "desk identity text",
                "beliefs": {"daily_stop_must_flatten": True},
                "recent_journal": [{"ts": "t", "kind": "trading_day", "pnl": 1.0}]}
        items = {it.key: it for it in items_from_snapshot(snapshot, desk,
                 {"suspected_day_type": "chop", "note": "smells like chop"})}
        for key in ["risk:account", "position:NVDA", "alert:NVDA:0",
                    "market:NVDA", "news:NVDA", "identity",
                    "belief:daily_stop_must_flatten", "journal:0", "hunch:day"]:
            self.assertIn(key, items)
        self.assertIn("NVDA", items["position:NVDA"].topics)
        # news stays bottom-priority: misleading by construction
        self.assertLess(items["news:NVDA"].priority,
                        items["market:NVDA"].priority + 0.001)


if __name__ == "__main__":
    unittest.main()
