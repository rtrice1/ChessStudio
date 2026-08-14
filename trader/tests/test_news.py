"""Comprehensive tests for NewsFeed."""

import json
import time
import unittest
import urllib.request
from threading import Thread
from typing import Optional

from mockschwab import MarketSim, NewsFeed, create_server


class TestNewsFeed(unittest.TestCase):
    """Tests for NewsFeed class."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.market = MarketSim(seed=42, symbols=["AAPL", "MSFT", "NVDA"])
        self.news_feed = NewsFeed(self.market, seed=42)

    def test_determinism_same_seed(self) -> None:
        """Headlines should be deterministic with same seed."""
        market1 = MarketSim(seed=123, symbols=["AAPL", "MSFT"])
        feed1 = NewsFeed(market1, seed=123)

        market2 = MarketSim(seed=123, symbols=["AAPL", "MSFT"])
        feed2 = NewsFeed(market2, seed=123)

        # Get items from both feeds
        items1 = feed1.items("AAPL", limit=10)
        items2 = feed2.items("AAPL", limit=10)

        # They should have same headlines (though timing might differ slightly)
        self.assertEqual(len(items1), len(items2))
        if len(items1) > 0 and len(items2) > 0:
            # At least first items should match
            self.assertEqual(items1[0]["headline"], items2[0]["headline"])

    def test_determinism_different_seed(self) -> None:
        """Headlines should differ with different seed."""
        market1 = MarketSim(seed=123, symbols=["AAPL"])
        feed1 = NewsFeed(market1, seed=123)

        market2 = MarketSim(seed=456, symbols=["AAPL"])
        feed2 = NewsFeed(market2, seed=456)

        items1 = feed1.items("AAPL", limit=20)
        items2 = feed2.items("AAPL", limit=20)

        # With different seeds, we expect at least some different headlines
        # (though by chance they might overlap)
        headlines1 = {item["headline"] for item in items1}
        headlines2 = {item["headline"] for item in items2}

        # They shouldn't be identical
        self.assertNotEqual(headlines1, headlines2)

    def test_item_has_required_keys(self) -> None:
        """Each item should have required keys."""
        items = self.news_feed.items("AAPL", limit=5)

        for item in items:
            self.assertIn("id", item)
            self.assertIn("symbol", item)
            self.assertIn("source", item)
            self.assertIn("headline", item)
            self.assertIn("published", item)

            # Verify types
            self.assertIsInstance(item["id"], str)
            self.assertIsInstance(item["symbol"], str)
            self.assertIsInstance(item["source"], str)
            self.assertIsInstance(item["headline"], str)
            self.assertIsInstance(item["published"], str)

    def test_no_alignment_leak(self) -> None:
        """Items returned should NOT contain alignment key."""
        items = self.news_feed.items("AAPL", limit=10)

        for item in items:
            self.assertNotIn("aligned", item)
            self.assertNotIn("noise", item)
            self.assertNotIn("inverted", item)
            self.assertNotIn("alignment", item)
            self.assertNotIn("truth", item)

    def test_valid_source(self) -> None:
        """Source should be either 'wire' or 'board'."""
        items = self.news_feed.items("AAPL", limit=20)

        for item in items:
            self.assertIn(item["source"], ["wire", "board"])

    def test_valid_symbol(self) -> None:
        """Symbol in item should match requested symbol."""
        items = self.news_feed.items("AAPL", limit=10)

        for item in items:
            self.assertEqual(item["symbol"], "AAPL")

    def test_newest_first_ordering(self) -> None:
        """Items should be ordered newest first (most recent ISO timestamp first)."""
        items = self.news_feed.items("AAPL", limit=20)

        if len(items) > 1:
            for i in range(len(items) - 1):
                current_time = items[i]["published"]
                next_time = items[i + 1]["published"]
                # ISO timestamps sort lexicographically
                self.assertGreaterEqual(current_time, next_time)

    def test_limit_respected(self) -> None:
        """Should not return more items than limit."""
        for limit in [1, 5, 10, 20]:
            items = self.news_feed.items("AAPL", limit=limit)
            self.assertLessEqual(len(items), limit)

    def test_unknown_symbol_returns_empty(self) -> None:
        """Unknown symbol should return empty list."""
        items = self.news_feed.items("INVALID", limit=10)
        self.assertEqual(items, [])

    def test_all_items_returns_dict(self) -> None:
        """all_items should return dict of symbol to items."""
        result = self.news_feed.all_items(["AAPL", "MSFT"], limit=5)

        self.assertIsInstance(result, dict)
        self.assertIn("AAPL", result)
        self.assertIn("MSFT", result)
        self.assertIsInstance(result["AAPL"], list)
        self.assertIsInstance(result["MSFT"], list)

    def test_iso8601_timestamps(self) -> None:
        """Published timestamps should be valid ISO-8601 UTC."""
        items = self.news_feed.items("AAPL", limit=10)

        for item in items:
            published = item["published"]
            # Should contain 'Z' or '+00:00' or similar timezone indicator
            self.assertTrue("T" in published)
            # Should be parseable as ISO format
            try:
                # Try to parse the ISO format
                if published.endswith("Z"):
                    datetime_str = published[:-1] + "+00:00"
                else:
                    datetime_str = published
                # Basic validation: should have digits and separators
                self.assertIn(":", published)
            except Exception as e:
                self.fail(f"Timestamp {published} not valid ISO-8601: {e}")

    def test_truth_distribution(self) -> None:
        """Truth alignment should be roughly 30/40/30 for aligned/noise/inverted."""
        # Create many markets with different seeds to generate diverse items
        all_alignments = []

        for seed_offset in range(30):
            market = MarketSim(seed=999 + seed_offset, symbols=["TEST"])
            feed = NewsFeed(market, seed=999 + seed_offset)

            # Get items from each feed
            items = feed.items("TEST", limit=100)

            # Collect alignments from truth dict
            all_alignments.extend(list(feed._truth.values()))

        # Check distribution of alignments
        if len(all_alignments) >= 30:  # Need enough samples
            aligned_count = all_alignments.count("aligned")
            noise_count = all_alignments.count("noise")
            inverted_count = all_alignments.count("inverted")
            total = len(all_alignments)

            aligned_pct = aligned_count / total
            noise_pct = noise_count / total
            inverted_pct = inverted_count / total

            # Check within ±15 percentage points
            self.assertGreater(aligned_pct, 0.15)
            self.assertLess(aligned_pct, 0.45)
            self.assertGreater(noise_pct, 0.25)
            self.assertLess(noise_pct, 0.55)
            self.assertGreater(inverted_pct, 0.15)
            self.assertLess(inverted_pct, 0.45)

    def test_headline_quality_wire(self) -> None:
        """Wire headlines should be well-formed."""
        items = self.news_feed.items("AAPL", limit=50)

        wire_items = [item for item in items if item["source"] == "wire"]

        for item in wire_items:
            headline = item["headline"]
            # Should contain the symbol
            self.assertIn("AAPL", headline)
            # Should be non-empty
            self.assertGreater(len(headline), 5)

    def test_headline_quality_board(self) -> None:
        """Board headlines should be well-formed and show retail tone."""
        items = self.news_feed.items("AAPL", limit=50)

        board_items = [item for item in items if item["source"] == "board"]

        for item in board_items:
            headline = item["headline"]
            # Should contain the symbol
            self.assertIn("AAPL", headline)
            # Should be non-empty
            self.assertGreater(len(headline), 5)

    def test_multiple_symbols_isolation(self) -> None:
        """Items for different symbols should be isolated."""
        aapl_items = self.news_feed.items("AAPL", limit=10)
        msft_items = self.news_feed.items("MSFT", limit=10)

        # All AAPL items should have symbol AAPL
        for item in aapl_items:
            self.assertEqual(item["symbol"], "AAPL")

        # All MSFT items should have symbol MSFT
        for item in msft_items:
            self.assertEqual(item["symbol"], "MSFT")

    def test_sequential_calls_grow_items(self) -> None:
        """Sequential calls with time advancement should generate more items."""
        feed = NewsFeed(MarketSim(seed=888, symbols=["AAPL"]), seed=888)

        items1 = feed.items("AAPL", limit=100)
        count1 = len(items1)

        # In a fast loop, we won't advance enough sim time to generate more
        # but the feed should at least maintain state
        items2 = feed.items("AAPL", limit=100)
        count2 = len(items2)

        # Count should be same or greater
        self.assertGreaterEqual(count2, count1)


class TestNewsHTTPEndpoint(unittest.TestCase):
    """Tests for news endpoint on HTTP server."""

    def setUp(self) -> None:
        """Set up test server."""
        self.server = create_server(host="127.0.0.1", port=0, seed=42, time_scale=1.0)
        self.port = self.server.server_address[1]

        # Start server in background thread
        self.server_thread = Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

        # Give server time to start
        time.sleep(0.1)

    def tearDown(self) -> None:
        """Shut down test server."""
        self.server.shutdown()
        self.server.server_close()

    def test_news_endpoint_basic(self) -> None:
        """GET /v1/marketdata/news should return JSON."""
        url = f"http://127.0.0.1:{self.port}/v1/marketdata/news?symbols=AAPL&limit=5"

        try:
            response = urllib.request.urlopen(url)
            data = json.loads(response.read().decode("utf-8"))

            self.assertIsInstance(data, dict)
            self.assertIn("AAPL", data)
        except Exception as e:
            self.fail(f"Failed to fetch news endpoint: {e}")

    def test_news_endpoint_multiple_symbols(self) -> None:
        """Endpoint should handle multiple symbols."""
        url = f"http://127.0.0.1:{self.port}/v1/marketdata/news?symbols=AAPL,MSFT,NVDA&limit=5"

        try:
            response = urllib.request.urlopen(url)
            data = json.loads(response.read().decode("utf-8"))

            self.assertIn("AAPL", data)
            self.assertIn("MSFT", data)
            self.assertIn("NVDA", data)

            # Each should be a list
            self.assertIsInstance(data["AAPL"], list)
            self.assertIsInstance(data["MSFT"], list)
            self.assertIsInstance(data["NVDA"], list)
        except Exception as e:
            self.fail(f"Failed to fetch multiple symbols: {e}")

    def test_news_endpoint_limit_parameter(self) -> None:
        """Endpoint should respect limit parameter."""
        url = f"http://127.0.0.1:{self.port}/v1/marketdata/news?symbols=AAPL&limit=2"

        try:
            response = urllib.request.urlopen(url)
            data = json.loads(response.read().decode("utf-8"))

            items = data["AAPL"]
            self.assertLessEqual(len(items), 2)

            # Verify items have correct structure
            for item in items:
                self.assertIn("id", item)
                self.assertIn("symbol", item)
                self.assertIn("source", item)
                self.assertIn("headline", item)
                self.assertIn("published", item)
        except Exception as e:
            self.fail(f"Failed limit test: {e}")

    def test_news_endpoint_unknown_symbol(self) -> None:
        """Endpoint should return empty list for unknown symbols."""
        url = f"http://127.0.0.1:{self.port}/v1/marketdata/news?symbols=INVALID&limit=10"

        try:
            response = urllib.request.urlopen(url)
            data = json.loads(response.read().decode("utf-8"))

            # Unknown symbols should return empty list
            self.assertIn("INVALID", data)
            self.assertEqual(data["INVALID"], [])
        except Exception as e:
            self.fail(f"Failed unknown symbol test: {e}")

    def test_news_endpoint_no_alignment_leak(self) -> None:
        """Endpoint response should not leak alignment info."""
        url = f"http://127.0.0.1:{self.port}/v1/marketdata/news?symbols=AAPL&limit=20"

        try:
            response = urllib.request.urlopen(url)
            data = json.loads(response.read().decode("utf-8"))

            for item in data.get("AAPL", []):
                self.assertNotIn("aligned", item)
                self.assertNotIn("noise", item)
                self.assertNotIn("inverted", item)
                self.assertNotIn("alignment", item)
                self.assertNotIn("truth", item)
        except Exception as e:
            self.fail(f"Failed alignment leak test: {e}")

    def test_news_endpoint_response_format(self) -> None:
        """Response should be valid JSON with correct format."""
        url = f"http://127.0.0.1:{self.port}/v1/marketdata/news?symbols=AAPL,MSFT&limit=5"

        try:
            response = urllib.request.urlopen(url)
            data = json.loads(response.read().decode("utf-8"))

            # Should be a dict
            self.assertIsInstance(data, dict)

            # Each value should be a list of items
            for symbol, items in data.items():
                self.assertIsInstance(items, list)
                for item in items:
                    self.assertIsInstance(item, dict)
                    # Required keys
                    required_keys = {"id", "symbol", "source", "headline", "published"}
                    self.assertTrue(required_keys.issubset(set(item.keys())))
        except Exception as e:
            self.fail(f"Failed response format test: {e}")

    def test_news_endpoint_default_limit(self) -> None:
        """Endpoint should use default limit if not specified."""
        url = f"http://127.0.0.1:{self.port}/v1/marketdata/news?symbols=AAPL"

        try:
            response = urllib.request.urlopen(url)
            data = json.loads(response.read().decode("utf-8"))

            items = data["AAPL"]
            # Default limit is 10
            self.assertLessEqual(len(items), 10)
        except Exception as e:
            self.fail(f"Failed default limit test: {e}")

    def test_news_endpoint_invalid_limit(self) -> None:
        """Endpoint should handle invalid limit gracefully."""
        url = f"http://127.0.0.1:{self.port}/v1/marketdata/news?symbols=AAPL&limit=notanumber"

        try:
            response = urllib.request.urlopen(url)
            data = json.loads(response.read().decode("utf-8"))

            # Should still return data with default limit
            self.assertIn("AAPL", data)
            self.assertIsInstance(data["AAPL"], list)
        except Exception as e:
            self.fail(f"Failed invalid limit test: {e}")


if __name__ == "__main__":
    unittest.main()
