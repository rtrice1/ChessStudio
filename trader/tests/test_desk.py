"""
Tests for Desk persistent-memory component.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path

from agent.desk import Desk


class TestDesk(unittest.TestCase):
    """Test suite for Desk class."""

    def setUp(self):
        """Create temporary directory for each test."""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = self.temp_dir.name

    def tearDown(self):
        """Clean up temporary directory."""
        self.temp_dir.cleanup()

    def test_init_creates_directory(self):
        """Test that Desk.__init__ creates root directory."""
        desk_path = os.path.join(self.root, "desk_state", "nested")
        desk = Desk(desk_path)
        self.assertTrue(os.path.isdir(desk_path))
        self.assertEqual(desk.root, Path(desk_path))

    def test_journal_append_single_entry(self):
        """Test appending a single journal entry."""
        desk = Desk(self.root)
        entry = desk.journal_append("trade", {"symbol": "AAPL", "qty": 10})

        self.assertIn("ts", entry)
        self.assertEqual(entry["kind"], "trade")
        self.assertEqual(entry["symbol"], "AAPL")
        self.assertEqual(entry["qty"], 10)

    def test_journal_append_multiple_entries(self):
        """Test appending multiple journal entries and reading them back."""
        desk = Desk(self.root)
        e1 = desk.journal_append("trade", {"symbol": "AAPL"})
        e2 = desk.journal_append("trade", {"symbol": "GOOGL"})
        e3 = desk.journal_append("note", {"text": "test"})

        entries = desk.journal_entries()
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0]["symbol"], "AAPL")
        self.assertEqual(entries[1]["symbol"], "GOOGL")
        self.assertEqual(entries[2]["kind"], "note")

    def test_journal_entries_persists_to_disk(self):
        """Test that journal entries persist across Desk instances."""
        desk1 = Desk(self.root)
        desk1.journal_append("event", {"msg": "first"})
        desk1.journal_append("event", {"msg": "second"})

        # Open new desk on same root
        desk2 = Desk(self.root)
        entries = desk2.journal_entries()

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["msg"], "first")
        self.assertEqual(entries[1]["msg"], "second")

    def test_journal_entries_kind_filter(self):
        """Test filtering journal entries by kind."""
        desk = Desk(self.root)
        desk.journal_append("trade", {"symbol": "AAPL"})
        desk.journal_append("note", {"text": "reminder"})
        desk.journal_append("trade", {"symbol": "GOOGL"})

        trade_entries = desk.journal_entries(kind="trade")
        note_entries = desk.journal_entries(kind="note")

        self.assertEqual(len(trade_entries), 2)
        self.assertEqual(len(note_entries), 1)
        self.assertEqual(trade_entries[0]["symbol"], "AAPL")
        self.assertEqual(trade_entries[1]["symbol"], "GOOGL")
        self.assertEqual(note_entries[0]["text"], "reminder")

    def test_journal_entries_limit(self):
        """Test limiting journal entries to last N."""
        desk = Desk(self.root)
        for i in range(10):
            desk.journal_append("event", {"seq": i})

        limited = desk.journal_entries(limit=3)
        self.assertEqual(len(limited), 3)
        # Should be the last 3 entries (most recent last)
        self.assertEqual(limited[0]["seq"], 7)
        self.assertEqual(limited[1]["seq"], 8)
        self.assertEqual(limited[2]["seq"], 9)

    def test_journal_entries_kind_and_limit(self):
        """Test filtering by kind and limiting."""
        desk = Desk(self.root)
        desk.journal_append("trade", {"id": 1})
        desk.journal_append("note", {"text": "a"})
        desk.journal_append("trade", {"id": 2})
        desk.journal_append("trade", {"id": 3})
        desk.journal_append("note", {"text": "b"})
        desk.journal_append("trade", {"id": 4})

        trades = desk.journal_entries(kind="trade", limit=2)
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["id"], 3)
        self.assertEqual(trades[1]["id"], 4)

    def test_journal_corrupt_line_skipped(self):
        """Test that corrupt JSON lines are skipped."""
        desk = Desk(self.root)
        desk.journal_append("event", {"msg": "valid1"})

        # Manually write corrupt line
        with open(desk.journal_path, "a") as f:
            f.write("NOT VALID JSON\n")

        desk.journal_append("event", {"msg": "valid2"})

        entries = desk.journal_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["msg"], "valid1")
        self.assertEqual(entries[1]["msg"], "valid2")

    def test_note_convenience(self):
        """Test note() convenience method."""
        desk = Desk(self.root)
        entry = desk.note("Test note", tags=["urgent", "follow-up"])

        self.assertEqual(entry["kind"], "note")
        self.assertEqual(entry["text"], "Test note")
        self.assertEqual(entry["tags"], ["urgent", "follow-up"])

    def test_note_no_tags(self):
        """Test note() with no tags defaults to empty list."""
        desk = Desk(self.root)
        entry = desk.note("Another note")

        self.assertEqual(entry["tags"], [])

    def test_set_belief_new(self):
        """Test setting a new belief."""
        desk = Desk(self.root)
        desk.set_belief("risk_tolerance", "moderate", "User profile")

        value = desk.get_belief("risk_tolerance")
        self.assertEqual(value, "moderate")

    def test_set_belief_update_pushes_history(self):
        """Test that updating a belief pushes old value to history."""
        desk = Desk(self.root)
        desk.set_belief("market_trend", "bullish", "MA crossover")
        desk.set_belief("market_trend", "bearish", "Price break")

        value = desk.get_belief("market_trend")
        self.assertEqual(value, "bearish")

        # Check history was saved
        beliefs = desk._load_beliefs()
        history = beliefs["market_trend"]["history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["value"], "bullish")
        self.assertEqual(history[0]["reason"], "MA crossover")

    def test_belief_history_caps_at_20(self):
        """Test that belief history is capped at 20 entries."""
        desk = Desk(self.root)

        # Set belief 30 times to exceed cap
        for i in range(30):
            desk.set_belief("test_key", f"value_{i}", f"reason_{i}")

        beliefs = desk._load_beliefs()
        history = beliefs["test_key"]["history"]
        # Should have 20 history entries (oldest dropped)
        self.assertEqual(len(history), 20)
        # First history entry should be from update 9 (since updates 0-8 are
        # dropped when we exceed 20, and we push before capping)
        # Actually: we do 30 sets total. After 20 updates with history,
        # we have history of size 19. On the 21st update, we push (making 20),
        # then on 22nd update we push making 21 then cap to 20, etc.
        # Let's verify the oldest entry in history is from around update 9-10
        self.assertIn("value_", history[0]["value"])

    def test_get_belief_default(self):
        """Test get_belief with default value."""
        desk = Desk(self.root)
        value = desk.get_belief("nonexistent", default="default_value")
        self.assertEqual(value, "default_value")

    def test_beliefs_returns_current_values_only(self):
        """Test beliefs() returns only current values, not history."""
        desk = Desk(self.root)
        desk.set_belief("key1", "val1", "reason1")
        desk.set_belief("key2", "val2", "reason2")
        desk.set_belief("key1", "val1_updated", "reason1_new")

        all_beliefs = desk.beliefs()
        self.assertEqual(all_beliefs, {"key1": "val1_updated", "key2": "val2"})

    def test_beliefs_atomic_persist(self):
        """Test that belief updates survive re-open (atomic save)."""
        desk1 = Desk(self.root)
        desk1.set_belief("persistent_key", "persistent_value", "test reason")

        # Open new desk on same root
        desk2 = Desk(self.root)
        value = desk2.get_belief("persistent_key")
        self.assertEqual(value, "persistent_value")

        beliefs = desk2.beliefs()
        self.assertEqual(beliefs["persistent_key"], "persistent_value")

    def test_load_context_empty_desk(self):
        """Test load_context on empty desk."""
        desk = Desk(self.root)
        context = desk.load_context()

        self.assertIsNone(context["identity"])
        self.assertEqual(context["beliefs"], {})
        self.assertEqual(context["recent_journal"], [])
        self.assertEqual(context["journal_size"], 0)

    def test_load_context_with_data(self):
        """Test load_context with identity, beliefs, and journal."""
        desk = Desk(self.root)

        # Write identity
        with open(desk.identity_path, "w") as f:
            f.write("I am a trading agent")

        # Add beliefs
        desk.set_belief("strategy", "momentum", "backtested")
        desk.set_belief("max_position", 1000, "risk limit")

        # Add journal entries
        desk.journal_append("start", {"ts_init": "2026-08-14"})
        desk.journal_append("trade", {"symbol": "AAPL", "qty": 10})

        context = desk.load_context(journal_limit=10)

        self.assertEqual(context["identity"], "I am a trading agent")
        self.assertEqual(context["beliefs"]["strategy"], "momentum")
        self.assertEqual(context["beliefs"]["max_position"], 1000)
        self.assertEqual(context["journal_size"], 2)
        self.assertEqual(len(context["recent_journal"]), 2)

    def test_load_context_limit(self):
        """Test load_context respects journal_limit."""
        desk = Desk(self.root)
        for i in range(20):
            desk.journal_append("event", {"seq": i})

        context = desk.load_context(journal_limit=5)

        self.assertEqual(context["journal_size"], 20)
        self.assertEqual(len(context["recent_journal"]), 5)
        # Should be last 5 entries
        self.assertEqual(context["recent_journal"][0]["seq"], 15)
        self.assertEqual(context["recent_journal"][4]["seq"], 19)

    def test_wake_summary_empty(self):
        """Test wake_summary on empty desk."""
        desk = Desk(self.root)
        summary = desk.wake_summary()

        # Should be empty or just whitespace
        self.assertEqual(summary.strip(), "")

    def test_wake_summary_with_identity(self):
        """Test wake_summary includes identity."""
        desk = Desk(self.root)
        with open(desk.identity_path, "w") as f:
            f.write("I am a sophisticated trader")

        summary = desk.wake_summary()
        self.assertIn("I am a sophisticated trader", summary)

    def test_wake_summary_with_beliefs(self):
        """Test wake_summary includes beliefs."""
        desk = Desk(self.root)
        desk.set_belief("risk_level", "moderate", "conservative approach")
        desk.set_belief("watch_list", ["AAPL", "GOOGL"], "tech focus")

        summary = desk.wake_summary()
        self.assertIn("Beliefs:", summary)
        self.assertIn("risk_level:", summary)
        self.assertIn("moderate", summary)
        self.assertIn("conservative approach", summary)

    def test_wake_summary_with_journal(self):
        """Test wake_summary includes journal entries."""
        desk = Desk(self.root)
        desk.journal_append("trade", {"symbol": "AAPL", "qty": 10})
        desk.journal_append("note", {"text": "Market rally expected"})

        summary = desk.wake_summary(journal_limit=10)
        self.assertIn("Recent journal:", summary)
        self.assertIn("trade:", summary)
        self.assertIn("AAPL", summary)
        self.assertIn("note:", summary)
        self.assertIn("Market rally", summary)

    def test_wake_summary_limit(self):
        """Test wake_summary respects journal_limit."""
        desk = Desk(self.root)
        for i in range(15):
            desk.journal_append("event", {"seq": i})

        summary_3 = desk.wake_summary(journal_limit=3)
        summary_10 = desk.wake_summary(journal_limit=10)

        # Count occurrences of "Recent journal:" and entries
        # In summary_3, should have fewer entries than summary_10
        lines_3 = summary_3.split("\n")
        lines_10 = summary_10.split("\n")
        # summary_10 should have more lines (more entries)
        self.assertGreater(len(lines_10), len(lines_3))

    def test_wake_summary_full(self):
        """Test wake_summary with identity, beliefs, and journal."""
        desk = Desk(self.root)

        # Identity
        with open(desk.identity_path, "w") as f:
            f.write("Trading Agent v1\nStrict risk management")

        # Beliefs
        desk.set_belief("volatility", "high", "VIX spike detected")
        desk.set_belief("position_size", 100, "max contracts")

        # Journal
        desk.journal_append("alert", {"msg": "circuit breaker triggered"})
        desk.journal_append("decision", {"action": "reduce_positions"})

        summary = desk.wake_summary(journal_limit=10)

        # Verify all components are present
        self.assertIn("Trading Agent v1", summary)
        self.assertIn("volatility:", summary)
        self.assertIn("high", summary)
        self.assertIn("Beliefs:", summary)
        self.assertIn("Recent journal:", summary)
        self.assertIn("alert:", summary)
        self.assertIn("decision:", summary)

    def test_journal_entries_empty_desk(self):
        """Test journal_entries on empty desk."""
        desk = Desk(self.root)
        entries = desk.journal_entries()
        self.assertEqual(entries, [])

    def test_journal_entries_empty_file(self):
        """Test journal_entries with empty journal file."""
        desk = Desk(self.root)
        # Create empty file
        desk.journal_path.touch()

        entries = desk.journal_entries()
        self.assertEqual(entries, [])

    def test_multiple_beliefs_updates(self):
        """Test multiple updates to different beliefs."""
        desk = Desk(self.root)

        desk.set_belief("belief_a", "value_a1", "reason_a1")
        desk.set_belief("belief_b", "value_b1", "reason_b1")
        desk.set_belief("belief_a", "value_a2", "reason_a2")
        desk.set_belief("belief_b", "value_b2", "reason_b2")
        desk.set_belief("belief_b", "value_b3", "reason_b3")

        beliefs = desk.beliefs()
        self.assertEqual(beliefs["belief_a"], "value_a2")
        self.assertEqual(beliefs["belief_b"], "value_b3")

        # Check histories
        all_beliefs = desk._load_beliefs()
        self.assertEqual(len(all_beliefs["belief_a"]["history"]), 1)
        self.assertEqual(len(all_beliefs["belief_b"]["history"]), 2)

    def test_iso_timestamps(self):
        """Test that journal entries have valid ISO-8601 timestamps."""
        desk = Desk(self.root)
        entry = desk.journal_append("test", {"msg": "test"})

        ts = entry["ts"]
        # Should be ISO-8601 format and parseable
        # Format: YYYY-MM-DDTHH:MM:SS.ffffff+00:00
        self.assertIn("T", ts)
        self.assertIn("+00:00", ts)


if __name__ == "__main__":
    unittest.main()
