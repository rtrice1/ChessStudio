"""Tests for look-ahead-free news impact measurement."""
import unittest

from agent.impact import forward_return, news_impact, scoreboard


def candles_at(times_and_prices):
    return [{"open": p, "high": p * 1.001, "low": p * 0.999, "close": p,
             "volume": 1000, "datetime": t} for t, p in times_and_prices]


DAY = [(f"2026-08-14T{h:02d}:{m:02d}:00+00:00", price)
       for (h, m), price in zip(
           [(14, 35), (14, 40), (14, 45), (14, 50), (14, 55),
            (15, 0), (15, 5), (15, 10)],
           # big move UP before 14:50, then decline after
           [100.0, 110.0, 120.0, 120.0, 118.0, 116.0, 114.0, 112.0])]


class TestForwardReturn(unittest.TestCase):
    def test_anchors_at_first_candle_after_publication(self):
        result = forward_return(candles_at(DAY), "2026-08-14T14:48:00+00:00",
                                bars_after=3)
        self.assertEqual(result["anchor_time"], "2026-08-14T14:50:00+00:00")
        self.assertEqual(result["baseline"], 120.0)

    def test_pre_news_move_is_structurally_excluded(self):
        # The 20% rally happened BEFORE publication; only the post-news
        # decline may be measured. A look-ahead bug would report a gain.
        result = forward_return(candles_at(DAY), "2026-08-14T14:48:00+00:00",
                                bars_after=3)
        self.assertLess(result["ret"], 0)

    def test_news_newer_than_all_candles_returns_none(self):
        self.assertIsNone(forward_return(candles_at(DAY),
                                         "2026-08-14T15:11:00+00:00"))

    def test_only_anchor_bar_exists_returns_none(self):
        self.assertIsNone(forward_return(candles_at(DAY),
                                         "2026-08-14T15:07:00+00:00"))

    def test_clips_to_available_bars(self):
        result = forward_return(candles_at(DAY), "2026-08-14T14:58:00+00:00",
                                bars_after=50)
        self.assertEqual(result["bars_used"], 2)  # 15:00 anchor -> 15:10 end

    def test_exact_timestamp_match_is_anchor(self):
        result = forward_return(candles_at(DAY), "2026-08-14T15:00:00+00:00",
                                bars_after=2)
        self.assertEqual(result["anchor_time"], "2026-08-14T15:00:00+00:00")


class TestNewsImpactAndScoreboard(unittest.TestCase):
    def test_items_measured_and_unmeasurable_marked(self):
        items = [
            {"id": "1", "symbol": "AAPL", "source": "wire",
             "headline": "AAPL slips", "published": "2026-08-14T14:48:00+00:00"},
            {"id": "2", "symbol": "AAPL", "source": "board",
             "headline": "AAPL to the moon", "published": "2026-08-14T15:11:00+00:00"},
        ]
        measured = news_impact(items, candles_at(DAY), bars_after=3)
        self.assertIsNotNone(measured[0]["forward"])
        self.assertIsNone(measured[1]["forward"])

    def test_scoreboard_by_source(self):
        measured = [
            {"source": "wire", "forward": {"ret": 0.01}},
            {"source": "wire", "forward": {"ret": -0.02}},
            {"source": "board", "forward": {"ret": -0.01}},
            {"source": "board", "forward": None},
        ]
        board = scoreboard(measured)
        self.assertEqual(board["wire"]["n"], 2)
        self.assertEqual(board["board"]["n"], 1)
        self.assertAlmostEqual(board["wire"]["hit_rate_up"], 0.5)


class TestMockFeedCausality(unittest.TestCase):
    def test_mock_feed_never_publishes_from_the_future(self):
        from mockschwab.market import MarketSim
        from mockschwab.news import NewsFeed
        sim = MarketSim(seed=11, time_scale=100_000.0)
        feed = NewsFeed(sim, seed=11)
        import time as _time
        _time.sleep(0.2)  # let sim time advance so items generate
        for symbol in ["AAPL", "MSFT"]:
            items = feed.items(symbol, limit=50)
            now = sim._sim_timestamp()  # sampled AFTER generation
            for item in items:
                from datetime import datetime
                self.assertLessEqual(datetime.fromisoformat(item["published"]),
                                     now, f"future-dated news: {item}")


if __name__ == "__main__":
    unittest.main()
