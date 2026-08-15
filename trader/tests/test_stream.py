"""Tests for the streamer cache, frame parsing, and REST fallback."""
import json
import time
import unittest
from unittest import mock

from agent.stream import (FRESH_SECONDS, QuoteCache, StreamingDataFeed,
                          parse_stream_message)


def eq_frame(**symbols):
    """Canned LEVELONE_EQUITIES data frame: eq_frame(AAPL=(bid,ask,last))."""
    return json.dumps({"data": [{"service": "LEVELONE_EQUITIES",
                                 "timestamp": 1755500000000,
                                 "content": [
                                     {"key": s, "1": b, "2": a, "3": l}
                                     for s, (b, a, l) in symbols.items()]}]})


class TestParseStreamMessage(unittest.TestCase):
    def setUp(self):
        self.cache = QuoteCache()

    def test_equity_frame_updates_cache(self):
        n = parse_stream_message(eq_frame(AAPL=(189.98, 190.02, 190.00)),
                                 self.cache)
        self.assertEqual(n, 1)
        quote = self.cache.fresh("AAPL")
        self.assertEqual(quote["bid"], 189.98)
        self.assertEqual(quote["last"], 190.00)
        self.assertIn("timestamp", quote)

    def test_option_frame_maps_padded_key(self):
        raw = json.dumps({"data": [{"service": "LEVELONE_OPTIONS", "content": [
            {"key": "AAPL  260821C00190000", "2": 3.0, "3": 3.2, "4": 3.1}]}]})
        self.assertEqual(parse_stream_message(raw, self.cache), 1)
        quote = self.cache.fresh("AAPL260821C00190000")
        self.assertEqual(quote["ask"], 3.2)

    def test_partial_update_merges(self):
        parse_stream_message(eq_frame(AAPL=(189.98, 190.02, 190.00)), self.cache)
        raw = json.dumps({"data": [{"service": "LEVELONE_EQUITIES", "content": [
            {"key": "AAPL", "3": 190.50}]}]})  # last only
        parse_stream_message(raw, self.cache)
        quote = self.cache.fresh("AAPL")
        self.assertEqual(quote["last"], 190.50)
        self.assertEqual(quote["bid"], 189.98)  # earlier fields survive

    def test_heartbeats_and_acks_ignored(self):
        for raw in ('{"notify":[{"heartbeat":"1755500000"}]}',
                    '{"response":[{"service":"ADMIN","command":"LOGIN"}]}',
                    "not json at all"):
            self.assertEqual(parse_stream_message(raw, self.cache), 0)

    def test_unknown_service_ignored(self):
        raw = json.dumps({"data": [{"service": "CHART_EQUITY",
                                    "content": [{"key": "AAPL", "1": 1.0}]}]})
        self.assertEqual(parse_stream_message(raw, self.cache), 0)


class TestQuoteCacheFreshness(unittest.TestCase):
    def test_stale_quotes_not_served(self):
        cache = QuoteCache()
        cache.update("AAPL", {"bid": 1.0, "ask": 2.0, "last": 1.5})
        self.assertIsNotNone(cache.fresh("AAPL"))
        cache._quotes["AAPL"]["_mono"] = time.monotonic() - FRESH_SECONDS - 1
        self.assertIsNone(cache.fresh("AAPL"))

    def test_no_last_price_means_not_fresh(self):
        cache = QuoteCache()
        cache.update("AAPL", {"bid": 1.0})
        self.assertIsNone(cache.fresh("AAPL"))


class TestStreamingDataFeed(unittest.TestCase):
    def feed(self):
        client = mock.Mock()
        client.quotes.return_value = {"MSFT": {"bid": 1, "ask": 2, "last": 1.5}}
        feed = StreamingDataFeed(client, ["AAPL", "MSFT"], enable_stream=False)
        return feed, client

    def test_fresh_cache_hits_skip_rest(self):
        feed, client = self.feed()
        feed.cache.update("AAPL", {"bid": 10.0, "ask": 10.1, "last": 10.05})
        out = feed.quotes(["AAPL"])
        self.assertEqual(out["AAPL"]["last"], 10.05)
        client.quotes.assert_not_called()
        self.assertEqual(feed.stats()["stream_hits"], 1)

    def test_stale_symbols_fall_back_to_rest(self):
        feed, client = self.feed()
        feed.cache.update("AAPL", {"bid": 10.0, "ask": 10.1, "last": 10.05})
        out = feed.quotes(["AAPL", "MSFT"])   # MSFT not in cache
        client.quotes.assert_called_once_with(["MSFT"])
        self.assertEqual(out["MSFT"]["last"], 1.5)
        self.assertEqual(feed.stats()["rest_fallbacks"], 1)

    def test_non_quote_calls_delegate(self):
        feed, client = self.feed()
        client.account.return_value = {"equity": 1.0}
        self.assertEqual(feed.account(), {"equity": 1.0})

    def test_orders_still_disabled_via_delegation(self):
        from agent.schwab import OrdersDisabled, SchwabClient
        real = SchwabClient.__new__(SchwabClient)
        feed = StreamingDataFeed(real, ["AAPL"], enable_stream=False)
        with self.assertRaises(OrdersDisabled):
            feed.place_order("AAPL", "BUY", 1)


if __name__ == "__main__":
    unittest.main()
