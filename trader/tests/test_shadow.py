"""Tests for shadow broker."""

import json
import tempfile
import unittest
from pathlib import Path

import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.shadow import DataFeed, ShadowBroker


class FakeDataClient:
    """Fake data client for testing."""

    def __init__(self):
        self.quotes_call_count = 0
        self.quotes_data = {
            "AAPL": {"bid": 150.00, "ask": 150.10, "last": 150.05},
            "SPY": {"bid": 450.00, "ask": 450.20, "last": 450.10},
            "MSFT": {"bid": 380.00, "ask": 380.30, "last": 380.15},
            # OCC-style option symbol
            "AAPL240119C00150000": {"bid": 5.00, "ask": 5.10, "last": 5.05},
        }

    def quotes(self, symbols):
        """Return quotes for symbols."""
        self.quotes_call_count += 1
        result = {}
        for symbol in symbols:
            if symbol in self.quotes_data:
                result[symbol] = self.quotes_data[symbol]
            # Missing symbols just aren't in the result dict
        return result

    def price_history(self, symbol, days, interval):
        """Return empty price history (not used in tests)."""
        return {"symbol": symbol, "candles": []}

    def chain(self, symbol, expiry=None):
        """Return empty chain (not used in tests)."""
        return {"symbol": symbol, "options": []}

    def __getattr__(self, name):
        """Raise error on unexpected attribute access (e.g., place_order)."""
        raise AssertionError(f"Unexpected attribute access on FakeDataClient: {name}")


class TestDataFeed(unittest.TestCase):
    """Test DataFeed quote adapter."""

    def test_quote_basic(self):
        """Test basic quote retrieval."""
        client = FakeDataClient()
        feed = DataFeed(client, ["AAPL", "SPY"])

        quote = feed.quote("AAPL")
        self.assertEqual(quote["bid"], 150.00)
        self.assertEqual(quote["ask"], 150.10)
        self.assertEqual(quote["last"], 150.05)

    def test_quote_caching(self):
        """Test quote caching within 2 seconds."""
        client = FakeDataClient()
        feed = DataFeed(client, ["AAPL"])

        # First call
        quote1 = feed.quote("AAPL")
        calls_after_first = client.quotes_call_count

        # Second call within 2 seconds should use cache
        quote2 = feed.quote("AAPL")
        calls_after_second = client.quotes_call_count

        self.assertEqual(calls_after_first, 1)
        self.assertEqual(calls_after_second, 1)  # No additional call
        self.assertEqual(quote1, quote2)

    def test_unknown_symbol(self):
        """Test unknown symbol returns error."""
        client = FakeDataClient()
        feed = DataFeed(client, ["AAPL", "SPY"])

        quote = feed.quote("UNKNOWN")
        self.assertIn("error", quote)
        self.assertIn("Unknown symbol", quote["error"])

    def test_occ_symbol(self):
        """Test OCC-style option symbol is accepted."""
        client = FakeDataClient()
        feed = DataFeed(client, ["AAPL"])

        quote = feed.quote("AAPL240119C00150000")
        self.assertNotIn("error", quote)
        self.assertEqual(quote["bid"], 5.00)
        self.assertEqual(quote["ask"], 5.10)
        self.assertEqual(quote["last"], 5.05)

    def test_occ_symbol_invalid(self):
        """Test invalid OCC format is rejected."""
        client = FakeDataClient()
        feed = DataFeed(client, ["AAPL"])

        # Invalid OCC format (too many letters, wrong length)
        quote = feed.quote("TOOLONGSTRIKE123456C12345678")
        self.assertIn("error", quote)


class TestShadowBroker(unittest.TestCase):
    """Test ShadowBroker order execution."""

    def setUp(self):
        self.client = FakeDataClient()
        self.broker = ShadowBroker(self.client, ["AAPL", "SPY", "MSFT"], starting_cash=100_000.0)

    def test_account_snapshot(self):
        """Test account snapshot."""
        snap = self.broker.account()
        self.assertEqual(snap["cash"], 100_000.00)
        self.assertEqual(snap["equity"], 100_000.00)
        self.assertEqual(snap["realizedPnl"], 0.0)
        self.assertEqual(len(snap["positions"]), 0)

    def test_buy_order_fills(self):
        """Test buying shares fills and decreases cash."""
        snap_before = self.broker.account()
        cash_before = snap_before["cash"]

        result = self.broker.place_order("AAPL", "BUY", 10, order_type="MARKET")
        self.assertEqual(result["status"], "FILLED")

        snap_after = self.broker.account()
        cash_after = snap_after["cash"]

        # Should have lost cash
        self.assertLess(cash_after, cash_before)

        # Should have position
        self.assertEqual(len(snap_after["positions"]), 1)
        pos = snap_after["positions"][0]
        self.assertEqual(pos["symbol"], "AAPL")
        self.assertEqual(pos["quantity"], 10)

    def test_buy_and_sell_round_trip(self):
        """Test buying then selling loses the spread."""
        snap_before = self.broker.account()
        cash_before = snap_before["cash"]
        realized_before = snap_before["realizedPnl"]

        # Buy 10 at ask (150.10)
        buy_result = self.broker.place_order("AAPL", "BUY", 10, order_type="MARKET")
        self.assertEqual(buy_result["status"], "FILLED")

        snap_mid = self.broker.account()

        # Sell 10 at bid (150.00)
        sell_result = self.broker.place_order("AAPL", "SELL", 10, order_type="MARKET")
        self.assertEqual(sell_result["status"], "FILLED")

        snap_after = self.broker.account()

        # Should have flat position
        self.assertEqual(len(snap_after["positions"]), 0)

        # Should have lost money to spread (bought at ask, sold at bid)
        cash_after = snap_after["cash"]
        realized_after = snap_after["realizedPnl"]

        self.assertLess(cash_after, cash_before)
        self.assertLess(realized_after, realized_before)

    def test_unknown_symbol_rejected(self):
        """Test order for unknown symbol is rejected."""
        snap_before = self.broker.account()
        cash_before = snap_before["cash"]

        result = self.broker.place_order("BADTICK", "BUY", 10, order_type="MARKET")
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("reason", result)

        snap_after = self.broker.account()
        cash_after = snap_after["cash"]

        # Cash should be unchanged
        self.assertEqual(cash_after, cash_before)

    def test_occ_symbol_buy(self):
        """Test buying an OCC-style option symbol."""
        result = self.broker.place_order("AAPL240119C00150000", "BUY", 1, order_type="MARKET")
        self.assertEqual(result["status"], "FILLED")

        snap = self.broker.account()
        self.assertEqual(len(snap["positions"]), 1)
        pos = snap["positions"][0]
        self.assertEqual(pos["symbol"], "AAPL240119C00150000")
        self.assertEqual(pos["quantity"], 1)

    def test_no_orders_reach_data_client(self):
        """Test that place_order never sends to data client."""
        # FakeDataClient.place_order doesn't exist; if it's called, __getattr__ raises
        result = self.broker.place_order("AAPL", "BUY", 10, order_type="MARKET")
        self.assertEqual(result["status"], "FILLED")

        # The above should not trigger any place_order call on data_client
        # If it did, the test would fail with AssertionError

    def test_save_and_load(self):
        """Test persisting and loading account state."""
        # Do a profitable round trip
        self.broker.place_order("AAPL", "BUY", 10, order_type="MARKET")
        snap_after_buy = self.broker.account()

        # Sell at a better price by faking a quote update
        # (we can't actually do this with FakeDataClient, so let's just test save/load)
        self.broker.place_order("AAPL", "SELL", 5, order_type="MARKET")

        snap_before_save = self.broker.account()
        cash_before_save = snap_before_save["cash"]
        realized_before_save = snap_before_save["realizedPnl"]

        # Save to temp file
        with tempfile.TemporaryDirectory() as tmpdir:
            book_path = f"{tmpdir}/test_book.json"
            self.broker.save(path=book_path)

            # Verify file exists and has correct content
            with open(book_path, "r") as f:
                saved_data = json.load(f)

            self.assertAlmostEqual(saved_data["cash"], cash_before_save, places=2)
            self.assertAlmostEqual(saved_data["realized_pnl"], realized_before_save, places=2)

            # Create new broker with same book path
            broker2 = ShadowBroker(self.client, ["AAPL", "SPY", "MSFT"], book_path=book_path)

            snap2 = broker2.account()
            self.assertAlmostEqual(snap2["cash"], cash_before_save, places=2)
            self.assertAlmostEqual(snap2["realizedPnl"], realized_before_save, places=2)

            # Open positions must survive the round trip (regression:
            # 2026-08-18, load dropped them — their cost vanished from
            # equity and the engine re-entered names it already held).
            self.assertEqual(len(snap2["positions"]), 1)
            pos = snap2["positions"][0]
            self.assertEqual((pos["symbol"], pos["quantity"]), ("AAPL", 5))
            # And equity prices the restored position, not just cash.
            self.assertGreater(snap2["equity"], snap2["cash"])

    def test_list_orders(self):
        """Test listing orders."""
        self.broker.place_order("AAPL", "BUY", 10, order_type="MARKET")
        orders = self.broker.list_orders()
        self.assertGreater(len(orders), 0)
        self.assertEqual(orders[0]["symbol"], "AAPL")
        self.assertEqual(orders[0]["status"], "FILLED")

    def test_cancel_order(self):
        """Test canceling a working limit order."""
        # Place a limit order that won't fill immediately
        result = self.broker.place_order("AAPL", "BUY", 10, order_type="LIMIT", price=140.00)

        if result["status"] == "WORKING":
            order_id = result["orderId"]
            cancel_result = self.broker.cancel_order(order_id)
            self.assertEqual(cancel_result["status"], "CANCELLED")

    def test_delegated_methods(self):
        """Test that data delegation methods work."""
        quotes = self.broker.quotes(["AAPL"])
        self.assertIn("AAPL", quotes)

        history = self.broker.price_history("AAPL", days=5, interval=60)
        self.assertIn("symbol", history)

        chain = self.broker.chain("AAPL")
        self.assertIn("symbol", chain)

        news = self.broker.news(["AAPL"])
        self.assertEqual(news, {})


if __name__ == "__main__":
    unittest.main()
