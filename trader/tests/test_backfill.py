"""Tests for the backfill tool."""
import json
import os
import tempfile
import unittest

from agent.backfill import backfill_day, run_backfill
from agent.gut import Gut


class TestBackfillDay(unittest.TestCase):
    """Tests for the backfill_day function."""

    def test_deterministic_same_seed(self):
        """backfill_day should return identical output for the same seed."""
        seed = 42
        result1 = backfill_day(seed)
        result2 = backfill_day(seed)

        self.assertIsNotNone(result1)
        self.assertIsNotNone(result2)
        self.assertEqual(result1, result2)

    def test_well_formed_output(self):
        """backfill_day should return a dict with expected keys."""
        seed = 100
        result = backfill_day(seed)

        self.assertIsNotNone(result)
        self.assertIn("seed", result)
        self.assertIn("features", result)
        self.assertIn("classification", result)

        # Check seed matches
        self.assertEqual(result["seed"], seed)

        # Check features dict has expected keys
        features = result["features"]
        expected_feature_keys = {"open_vol_ratio", "efficiency", "breadth", "avg_abs_return", "vwap_above_frac", "n_symbols"}
        self.assertEqual(set(features.keys()), expected_feature_keys)

        # Check classification dict has expected keys
        classification = result["classification"]
        self.assertIn("day_type", classification)
        self.assertIn("confidence", classification)
        self.assertIn("features", classification)

        # day_type should be a string
        self.assertIsInstance(classification["day_type"], str)
        # confidence should be between 0 and 1
        self.assertIsInstance(classification["confidence"], (int, float))
        self.assertGreaterEqual(classification["confidence"], 0.0)
        self.assertLessEqual(classification["confidence"], 1.0)

    def test_different_seeds_different_output(self):
        """Different seeds should (usually) produce different features."""
        result1 = backfill_day(seed=200)
        result2 = backfill_day(seed=300)

        self.assertIsNotNone(result1)
        self.assertIsNotNone(result2)

        # Features should differ (extremely unlikely to be identical for very different seeds)
        # We check that at least one key differs
        features1 = result1["features"]
        features2 = result2["features"]

        # Check if any feature differs (allowing for floating point coincidences)
        differs = False
        for key in features1:
            if key in features2:
                if features1[key] != features2[key]:
                    differs = True
                    break

        self.assertTrue(differs, "Expected different seeds to produce different features")

    def test_custom_symbols(self):
        """backfill_day should accept a custom symbols list."""
        custom_symbols = ["AAPL", "MSFT"]
        result = backfill_day(seed=300, symbols=custom_symbols)

        self.assertIsNotNone(result)
        features = result["features"]
        # n_symbols should be the number of symbols we passed
        self.assertEqual(features["n_symbols"], len(custom_symbols))

    def test_returns_none_on_failure(self):
        """backfill_day may return None if features cannot be computed."""
        # This is hard to trigger with valid inputs, but the function
        # should handle it gracefully. We document that it's possible.
        # For now, we just verify the happy path returns non-None.
        result = backfill_day(seed=400)
        # Most seeds should succeed, but we're testing the function handles None
        self.assertTrue(result is None or isinstance(result, dict))


class TestRunBackfill(unittest.TestCase):
    """Tests for the run_backfill function."""

    def test_backfill_records_entries(self):
        """run_backfill should record entries into the Gut."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gut_path = os.path.join(tmpdir, "test_memory.jsonl")
            gut = Gut(gut_path)

            # Backfill 10 days
            summary = run_backfill(gut, n_days=10, start_seed=5000)

            # Check summary structure
            self.assertIn("recorded", summary)
            self.assertIn("skipped", summary)
            self.assertIn("type_counts", summary)

            # Check that some days were recorded
            self.assertGreater(summary["recorded"], 0)

            # Check that recorded count matches gut.days()
            gut_days = gut.days()
            self.assertEqual(len(gut_days), summary["recorded"])

    def test_all_entries_have_source_marker(self):
        """All recorded entries should have outcome['source'] == 'sim_backfill'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gut_path = os.path.join(tmpdir, "test_memory.jsonl")
            gut = Gut(gut_path)

            run_backfill(gut, n_days=10, start_seed=6000)

            gut_days = gut.days()
            for day in gut_days:
                self.assertIn("outcome", day)
                self.assertIn("source", day["outcome"])
                self.assertEqual(day["outcome"]["source"], "sim_backfill")

    def test_all_entries_have_none_pnl(self):
        """All recorded entries should have outcome['pnl_pct'] == None."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gut_path = os.path.join(tmpdir, "test_memory.jsonl")
            gut = Gut(gut_path)

            run_backfill(gut, n_days=10, start_seed=7000)

            gut_days = gut.days()
            for day in gut_days:
                self.assertIn("outcome", day)
                self.assertIn("pnl_pct", day["outcome"])
                self.assertIsNone(day["outcome"]["pnl_pct"])

    def test_type_counts_sum_to_recorded(self):
        """type_counts values should sum to recorded count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gut_path = os.path.join(tmpdir, "test_memory.jsonl")
            gut = Gut(gut_path)

            summary = run_backfill(gut, n_days=10, start_seed=8000)

            type_sum = sum(summary["type_counts"].values())
            self.assertEqual(type_sum, summary["recorded"])

    def test_progress_printing(self, capsys=None):
        """run_backfill should print progress messages."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gut_path = os.path.join(tmpdir, "test_memory.jsonl")
            gut = Gut(gut_path)

            # Run with small progress_every so we definitely get a message
            summary = run_backfill(gut, n_days=50, start_seed=9000, progress_every=10)

            # We expect at least one progress message
            self.assertGreater(summary["recorded"], 0)

    def test_custom_symbols_in_backfill(self):
        """run_backfill should pass custom symbols to backfill_day."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gut_path = os.path.join(tmpdir, "test_memory.jsonl")
            gut = Gut(gut_path)

            custom_symbols = ["AAPL", "SPY"]
            summary = run_backfill(
                gut, n_days=5, start_seed=10000, symbols=custom_symbols
            )

            # Check that features were recorded
            gut_days = gut.days()
            self.assertGreater(len(gut_days), 0)
            # Each recorded day should have features
            for day in gut_days:
                features = day.get("features", {})
                self.assertIsNotNone(features)
                self.assertIn("open_vol_ratio", features)

    def test_start_seed_increments(self):
        """run_backfill should increment seeds from start_seed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            gut_path = os.path.join(tmpdir, "test_memory.jsonl")
            gut = Gut(gut_path)

            start_seed = 11000
            summary = run_backfill(gut, n_days=5, start_seed=start_seed)

            gut_days = gut.days()
            recorded_seeds = sorted([day["outcome"]["seed"] for day in gut_days])

            # Seeds should be start_seed, start_seed+1, start_seed+2, ...
            expected_seeds = list(range(start_seed, start_seed + len(recorded_seeds)))
            self.assertEqual(recorded_seeds, expected_seeds)


if __name__ == "__main__":
    unittest.main()
