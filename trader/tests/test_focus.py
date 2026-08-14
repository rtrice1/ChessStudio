"""Tests for the focus subsystem."""
import unittest

from agent.focus import (FocusSession, FocusState, Item, assess,
                         build_context, items_from_snapshot, salience)


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


class TestDetailRendering(unittest.TestCase):
    def items(self):
        return [
            Item("position:NVDA", "NVDA held", 0.9, topics=["NVDA"],
                 detail="NVDA x50 @180.00, upl -50, stop 177.3, target 184.5, "
                        "entered on ORB at 10:04, vwap 179.2 and holding above"),
            Item("market:XOM", "XOM quiet", 0.3, topics=["XOM"],
                 detail="XOM long detail that narrow focus should never render"),
        ]

    def test_narrow_renders_on_topic_detail(self):
        result = build_context(self.items(), FocusState(0.9, ["NVDA"]),
                               budget_chars=500)
        self.assertIn("entered on ORB at 10:04", result["text"])
        self.assertNotIn("never render", result["text"])

    def test_wide_renders_briefs_only(self):
        result = build_context(self.items(), FocusState(0.2, []),
                               budget_chars=500)
        self.assertIn("NVDA held", result["text"])
        self.assertNotIn("entered on ORB", result["text"])
        self.assertIn("XOM quiet", result["text"])


class TestFocusSession(unittest.TestCase):
    def test_task_lifecycle_general_to_specific_and_back(self):
        session = FocusSession()
        self.assertEqual(session.state.width, FocusSession.REST_WIDTH)

        started = session.start_task(["NVDA"], "reviewing NVDA setup")
        self.assertEqual(started.width, FocusSession.START_WIDTH)  # general first

        d1 = session.deepen("placing the trade")
        d2 = session.deepen("managing the position")
        self.assertGreater(d2.width, d1.width)
        self.assertGreater(d1.width, started.width)
        self.assertEqual(d2.topics, ["NVDA"])

        relaxed = session.relax("trade thesis unclear, stepping back")
        self.assertLess(relaxed.width, d2.width)

        ended = session.end_task()
        self.assertEqual(ended.width, FocusSession.REST_WIDTH)
        self.assertEqual(ended.topics, [])
        self.assertEqual([h["event"] for h in session.history],
                         ["start_task", "deepen", "deepen", "relax", "end_task"])

    def test_width_is_clamped(self):
        session = FocusSession()
        session.start_task(["A"])
        for _ in range(10):
            session.deepen()
        self.assertEqual(session.state.width, 1.0)
        for _ in range(10):
            session.relax()
        self.assertEqual(session.state.width, FocusSession.REST_WIDTH)

    def test_situation_seizes_focus_from_wide_trajectory(self):
        session = FocusSession()
        session.start_task(["XOM"], "idle review of XOM")
        pos = [{"symbol": "NVDA", "quantity": 50, "averagePrice": 180.0,
                "marketValue": 9000.0}]
        state = session.reassess(account(equity=98_000.0, positions=pos),
                                 [], None, 0.5, 100_000.0)
        self.assertEqual(state.width, 1.0)
        self.assertEqual(state.topics, ["NVDA"])  # the drawdown is the task now
        self.assertIn("seized", session.history[-1]["event"])

    def test_situation_does_not_widen_a_deep_task(self):
        session = FocusSession()
        session.start_task(["NVDA"])
        session.deepen(); session.deepen(); session.deepen()
        deep = session.state.width
        state = session.reassess(account(), [], None, 0.3, 100_000.0)
        self.assertEqual(state.width, deep)  # calm situation doesn't interrupt


if __name__ == "__main__":
    unittest.main()
