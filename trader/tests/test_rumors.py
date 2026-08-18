"""Tests for the overnight rumor scanner and its next-day backtrace."""
import io
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

from agent.rumors import (ET, MockBoardSource, RedditSource, aggregate,
                          calibration, context, extract_tickers, for_date,
                          grade, scan)

WATCH = ["AAPL", "TSLA", "NVDA", "SPY"]


def _http_response(payload: dict):
    """A context-manager stand-in for urlopen's response."""
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = mock.MagicMock(return_value=False)
    return resp


class TestRedditSourceAuthAndFailures(unittest.TestCase):
    def test_unauthenticated_failures_are_counted_not_raised(self):
        src = RedditSource(subreddits=["stocks", "options"])
        with mock.patch.dict(os.environ, {"REDDIT_CLIENT_ID": "",
                                          "REDDIT_CLIENT_SECRET": ""}), \
             mock.patch("agent.rumors.urllib.request.urlopen",
                        side_effect=OSError("403 blocked")):
            posts = src.fetch(WATCH)
        self.assertEqual(posts, [])
        self.assertEqual(src.fetch_errors, 2)   # one per subreddit

    def test_oauth_used_when_credentials_present(self):
        src = RedditSource(subreddits=["stocks"])
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if "access_token" in req.full_url:
                return _http_response({"access_token": "TOK123"})
            return _http_response({"data": {"children": [
                {"data": {"title": "$TSLA breakout", "selftext": "",
                          "created_utc": 1755500000}}]}})

        with mock.patch.dict(os.environ, {"REDDIT_CLIENT_ID": "cid",
                                          "REDDIT_CLIENT_SECRET": "sec"}), \
             mock.patch("agent.rumors.urllib.request.urlopen", fake_urlopen), \
             mock.patch("agent.rumors.time.sleep"):
            posts = src.fetch(WATCH)
        self.assertEqual(src.fetch_errors, 0)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["tickers"], ["TSLA"])
        token_req, listing_req = calls
        self.assertIn("www.reddit.com/api/v1/access_token", token_req.full_url)
        self.assertTrue(token_req.get_header("Authorization", "").startswith("Basic "))
        self.assertIn("oauth.reddit.com/r/stocks/new", listing_req.full_url)
        self.assertEqual(listing_req.get_header("Authorization"), "bearer TOK123")


class TestTickerExtraction(unittest.TestCase):
    def test_cashtags_and_bare_symbols(self):
        got = extract_tickers("$TSLA to the moon, NVDA earnings brewing", WATCH)
        self.assertEqual(got, {"TSLA", "NVDA"})

    def test_non_watchlist_ignored(self):
        self.assertEqual(extract_tickers("$GME and AMC squeeze", WATCH), set())

    def test_lowercase_cashtag_counts_bare_word_does_not(self):
        # $tsla is a deliberate ticker reference; "tsla" in prose is not.
        self.assertEqual(extract_tickers("$tsla calls", WATCH), {"TSLA"})
        self.assertEqual(extract_tickers("bought some tsla", WATCH), set())


class TestForDate(unittest.TestCase):
    def test_evening_scan_is_about_tomorrow(self):
        wed_2130 = datetime(2026, 8, 19, 21, 30, tzinfo=ET)
        self.assertEqual(for_date(wed_2130), "2026-08-20")

    def test_preopen_scan_is_about_today(self):
        thu_0545 = datetime(2026, 8, 20, 5, 45, tzinfo=ET)
        self.assertEqual(for_date(thu_0545), "2026-08-20")

    def test_friday_evening_rolls_to_monday(self):
        fri_2130 = datetime(2026, 8, 21, 21, 30, tzinfo=ET)
        self.assertEqual(for_date(fri_2130), "2026-08-24")


class TestAggregate(unittest.TestCase):
    def test_mention_floor_and_sentiment_sum(self):
        posts = [
            {"title": "TSLA breakout brewing", "tickers": ["TSLA"]},
            {"title": "TSLA calls loading", "tickers": ["TSLA"]},
            {"title": "TSLA bearish tomorrow", "tickers": ["TSLA"]},
            {"title": "AAPL mentioned once", "tickers": ["AAPL"]},
        ]
        agg = aggregate(posts)
        self.assertIn("TSLA", agg)
        self.assertNotIn("AAPL", agg)  # below MIN_MENTIONS
        self.assertEqual(agg["TSLA"]["mentions"], 3)
        self.assertEqual(agg["TSLA"]["sentiment"], 1)  # +1 +1 -1
        self.assertEqual(len(agg["TSLA"]["sample"]), 3)


class FakeSource:
    def __init__(self, posts):
        self.posts = posts

    def fetch(self, watch):
        return self.posts


class FakeClient:
    """price_history stub: TSLA up 2% on the graded day, NVDA down 1%."""
    MOVES = {"TSLA": (100.0, 102.0), "NVDA": (200.0, 198.0)}

    def price_history(self, symbol, days=5, interval=5):
        o, c = self.MOVES.get(symbol, (100.0, 100.0))
        return {"candles": [
            {"datetime": "2026-08-20T09:30:00", "open": o, "high": max(o, c),
             "low": min(o, c), "close": (o + c) / 2, "volume": 1000},
            {"datetime": "2026-08-20T15:55:00", "open": (o + c) / 2,
             "high": max(o, c), "low": min(o, c), "close": c, "volume": 1000},
        ]}

    def news(self, symbols, limit=10):
        return {}


class TestScanRecordsFetchErrors(unittest.TestCase):
    def test_blocked_scan_is_distinguishable_from_quiet(self):
        class DeadSource:
            fetch_errors = 4

            def fetch(self, watch):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rumors.jsonl")
            rec = scan([DeadSource()], WATCH, path,
                       now=datetime(2026, 8, 17, 21, 30, tzinfo=ET))
        self.assertEqual(rec["posts_seen"], 0)
        self.assertEqual(rec["fetch_errors"], 4)


class TestScanGradeCalibrate(unittest.TestCase):
    def run_scan(self, tmp):
        posts = [
            {"ts": "t", "title": "TSLA moon brewing", "tickers": ["TSLA"]},
            {"ts": "t", "title": "TSLA surge loading", "tickers": ["TSLA"]},
            {"ts": "t", "title": "NVDA bullish calls", "tickers": ["NVDA"]},
            {"ts": "t", "title": "NVDA upgrade coming", "tickers": ["NVDA"]},
        ]
        return scan([FakeSource(posts)], WATCH,
                    os.path.join(tmp, "rumors.jsonl"),
                    now=datetime(2026, 8, 19, 21, 30, tzinfo=ET))

    def test_scan_record_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec = self.run_scan(tmp)
            self.assertEqual(rec["for_date"], "2026-08-20")
            self.assertEqual(set(rec["tickers"]), {"TSLA", "NVDA"})
            with open(os.path.join(tmp, "rumors.jsonl")) as f:
                self.assertEqual(json.loads(f.readline())["kind"], "rumor_scan")

    def test_grade_hits_and_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_scan(tmp)
            grades = grade(os.path.join(tmp, "rumors.jsonl"),
                           os.path.join(tmp, "grades.jsonl"),
                           FakeClient(), today="2026-08-21")
            by_sym = {g["symbol"]: g for g in grades}
            # TSLA: crowd positive, moved +2% -> hit
            self.assertTrue(by_sym["TSLA"]["direction_hit"])
            # NVDA: crowd positive, moved -1% -> miss
            self.assertFalse(by_sym["NVDA"]["direction_hit"])
            # Grading twice adds nothing — the backtrace is idempotent.
            again = grade(os.path.join(tmp, "rumors.jsonl"),
                          os.path.join(tmp, "grades.jsonl"),
                          FakeClient(), today="2026-08-21")
            self.assertEqual(again, [])

    def test_future_sessions_not_graded(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_scan(tmp)
            grades = grade(os.path.join(tmp, "rumors.jsonl"),
                           os.path.join(tmp, "grades.jsonl"),
                           FakeClient(), today="2026-08-19")
            self.assertEqual(grades, [])

    def test_calibration_buckets(self):
        grades = (
            [{"sentiment": 3, "mentions": 8, "direction_hit": True,
              "abs_move_pct": 2.0}] * 3
            + [{"sentiment": 3, "mentions": 8, "direction_hit": False,
                "abs_move_pct": 1.0}]
            + [{"sentiment": -2, "mentions": 2, "direction_hit": True,
                "abs_move_pct": 0.5}]
        )
        cal = calibration(grades)
        self.assertEqual(cal["loud_positive"]["n"], 4)
        self.assertEqual(cal["loud_positive"]["direction_hit_rate"], 0.75)
        self.assertEqual(cal["quiet_negative"]["n"], 1)

    def test_context_bundles_scan_and_calibration(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(context(tmp))
            self.run_scan(tmp)
            grade(os.path.join(tmp, "rumors.jsonl"),
                  os.path.join(tmp, "rumor_grades.jsonl"),
                  FakeClient(), today="2026-08-21")
            ctx = context(tmp)
            self.assertIn("TSLA", ctx["scan"]["tickers"])
            # 2 mentions each -> the quiet bucket (loud needs >= 5)
            self.assertIn("quiet_positive", ctx["calibration"])

    def test_mock_board_source_shapes_posts(self):
        class BoardClient:
            def news(self, symbols, limit=10):
                return {"TSLA": [
                    {"ts": "t1", "source": "board", "headline": "TSLA moon"},
                    {"ts": "t2", "source": "wire", "headline": "TSLA earnings"},
                ]}
        posts = MockBoardSource(BoardClient()).fetch(WATCH)
        self.assertEqual(len(posts), 1)  # wire items are NOT rumors
        self.assertEqual(posts[0]["tickers"], ["TSLA"])


if __name__ == "__main__":
    unittest.main()
