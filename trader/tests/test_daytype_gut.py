"""Tests for day classification and gut memory."""
import os
import tempfile
import unittest

from agent.daytype import classify_day, day_features
from agent.gut import Gut


def candles_from_closes(closes, date="2026-08-14", volume=1000):
    out = []
    for i, close in enumerate(closes):
        prev = closes[i - 1] if i else close
        out.append({"open": prev, "high": max(prev, close) * 1.001,
                    "low": min(prev, close) * 0.999, "close": close,
                    "volume": volume,
                    "datetime": f"{date}T{9 + i // 12:02d}:{(i % 12) * 5:02d}:00+00:00"})
    return out


def trend_up_closes(n=40, start=100.0, step=0.15):
    return [start + i * step for i in range(n)]


def spike_settle_closes(n=40, start=100.0):
    # violent first 30 minutes, then a flat, quiet drift
    closes = [start, 101.5, 99.8, 101.2, 99.9, 101.0]
    level = 100.5
    for i in range(n - len(closes)):
        level += 0.02 if i % 2 == 0 else -0.02
        closes.append(level)
    return closes


class TestDayType(unittest.TestCase):
    def test_trend_up_day(self):
        candles = {s: candles_from_closes(trend_up_closes()) for s in ["A", "B", "C"]}
        features = day_features(candles)
        self.assertIsNotNone(features)
        result = classify_day(features)
        self.assertEqual(result["day_type"], "trend_up")
        self.assertGreater(features["efficiency"], 0.45)
        self.assertEqual(features["breadth"], 1.0)

    def test_trend_down_day(self):
        closes = [100.0 - i * 0.15 for i in range(40)]
        candles = {s: candles_from_closes(closes) for s in ["A", "B", "C"]}
        result = classify_day(day_features(candles))
        self.assertEqual(result["day_type"], "trend_down")

    def test_spike_then_settle_day(self):
        candles = {s: candles_from_closes(spike_settle_closes()) for s in ["A", "B"]}
        features = day_features(candles)
        result = classify_day(features)
        self.assertEqual(result["day_type"], "open_spike_settle")
        self.assertGreater(features["open_vol_ratio"], 1.8)

    def test_too_few_bars_is_none(self):
        candles = {"A": candles_from_closes([100.0, 100.1, 100.2])}
        self.assertIsNone(day_features(candles))


class TestGut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.gut = Gut(os.path.join(self.tmp.name, "day_memory.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def features(self, **overrides):
        base = {"open_vol_ratio": 1.0, "efficiency": 0.5, "breadth": 0.8,
                "avg_abs_return": 0.01, "vwap_above_frac": 0.7}
        base.update(overrides)
        return base

    def test_empty_gut_is_honest(self):
        hunch = self.gut.hunch(self.features())
        self.assertIsNone(hunch["suspected_day_type"])
        self.assertEqual(hunch["based_on"], 0)
        self.assertIn("no gut to trust", hunch["note"])

    def test_hunch_finds_similar_days(self):
        for _ in range(4):
            self.gut.record_day(self.features(), "trend_up",
                                {"pnl_pct": 0.01, "trades": 20})
        self.gut.record_day(
            self.features(open_vol_ratio=3.0, efficiency=0.1, breadth=0.5),
            "open_spike_settle", {"pnl_pct": -0.02, "trades": 30})

        hunch = self.gut.hunch(self.features(), k=3)
        self.assertEqual(hunch["suspected_day_type"], "trend_up")
        self.assertGreater(hunch["confidence"], 0.9)
        self.assertAlmostEqual(hunch["expected_pnl_pct"], 0.01, places=4)

        spike = self.features(open_vol_ratio=2.9, efficiency=0.12, breadth=0.5)
        hunch2 = self.gut.hunch(spike, k=1)
        self.assertEqual(hunch2["suspected_day_type"], "open_spike_settle")

    def test_similarity_ordering_and_persistence(self):
        self.gut.record_day(self.features(), "trend_up", {"pnl_pct": 0.01})
        self.gut.record_day(self.features(efficiency=0.05, breadth=0.5),
                            "chop", {"pnl_pct": -0.005})
        reopened = Gut(self.gut.path)
        similar = reopened.similar_days(self.features(), k=2)
        self.assertEqual(len(similar), 2)
        self.assertGreater(similar[0][0], similar[1][0])
        self.assertEqual(similar[0][1]["day_type"], "trend_up")

    def test_corrupt_line_skipped(self):
        self.gut.record_day(self.features(), "trend_up", {"pnl_pct": 0.01})
        with open(self.gut.path, "a") as f:
            f.write("not json\n")
        self.assertEqual(len(self.gut.days()), 1)


if __name__ == "__main__":
    unittest.main()
