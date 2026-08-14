"""Comprehensive tests for mockschwab trading system."""

import time
import unittest
from unittest.mock import patch

from mockschwab.accounts import AccountEngine
from mockschwab.market import MarketSim


class TestMarketSim(unittest.TestCase):
    """Tests for MarketSim."""

    def test_deterministic_quotes(self) -> None:
        """Quotes should be deterministic given same seed."""
        market1 = MarketSim(seed=42, symbols=["AAPL", "MSFT"])
        market2 = MarketSim(seed=42, symbols=["AAPL", "MSFT"])

        quote1_aapl = market1.quote("AAPL")
        quote2_aapl = market2.quote("AAPL")

        self.assertEqual(quote1_aapl["symbol"], "AAPL")
        self.assertEqual(quote1_aapl["last"], quote2_aapl["last"])
        self.assertEqual(quote1_aapl["bid"], quote2_aapl["bid"])
        self.assertEqual(quote1_aapl["ask"], quote2_aapl["ask"])

    def test_quote_structure(self) -> None:
        """Quotes should have correct structure."""
        market = MarketSim(seed=42, symbols=["AAPL"])
        quote = market.quote("AAPL")

        self.assertIn("symbol", quote)
        self.assertIn("bid", quote)
        self.assertIn("ask", quote)
        self.assertIn("last", quote)
        self.assertIn("timestamp", quote)

        self.assertEqual(quote["symbol"], "AAPL")
        self.assertGreater(quote["bid"], 0)
        self.assertGreater(quote["ask"], quote["bid"])
        self.assertGreater(quote["last"], 0)

    def test_unknown_symbol_quote(self) -> None:
        """Quote for unknown symbol should return error."""
        market = MarketSim(seed=42)
        quote = market.quote("INVALID")

        self.assertIn("error", quote)

    def test_price_history(self) -> None:
        """Price history should return OHLCV data."""
        market = MarketSim(seed=42, symbols=["AAPL"])
        history = market.price_history("AAPL", days=5, interval_minutes=60)

        self.assertIn("symbol", history)
        self.assertIn("candles", history)
        self.assertEqual(history["symbol"], "AAPL")
        self.assertGreater(len(history["candles"]), 0)

        candle = history["candles"][0]
        self.assertIn("open", candle)
        self.assertIn("high", candle)
        self.assertIn("low", candle)
        self.assertIn("close", candle)
        self.assertIn("volume", candle)
        self.assertIn("datetime", candle)

        # High should be >= low, high >= open/close, low <= open/close
        self.assertGreaterEqual(candle["high"], candle["low"])
        self.assertGreaterEqual(candle["high"], candle["open"])
        self.assertGreaterEqual(candle["high"], candle["close"])
        self.assertLessEqual(candle["low"], candle["open"])
        self.assertLessEqual(candle["low"], candle["close"])

    def test_unknown_symbol_price_history(self) -> None:
        """Price history for unknown symbol should return error."""
        market = MarketSim(seed=42)
        history = market.price_history("INVALID", days=5)

        self.assertIn("error", history)


class TestAccountEngine(unittest.TestCase):
    """Tests for AccountEngine."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.market = MarketSim(seed=42, symbols=["AAPL", "MSFT", "NVDA"])
        self.engine = AccountEngine(self.market, account_id="TEST-001", starting_cash=100_000.0)

    def test_initial_state(self) -> None:
        """Account should initialize with correct state."""
        self.assertEqual(self.engine.account_id, "TEST-001")
        self.assertEqual(self.engine.cash, 100_000.0)
        self.assertEqual(len(self.engine.positions), 0)
        self.assertEqual(len(self.engine.orders), 0)
        self.assertEqual(self.engine.realized_pnl, 0.0)

    def test_market_buy_order(self) -> None:
        """Market BUY order should fill immediately."""
        order = self.engine.place_order("AAPL", "BUY", 100, "MARKET")

        self.assertEqual(order["status"], "FILLED")
        self.assertIsNotNone(order["fillPrice"])
        self.assertIn("AAPL", self.engine.positions)
        self.assertEqual(self.engine.positions["AAPL"]["quantity"], 100)
        self.assertLess(self.engine.cash, 100_000.0)

    def test_market_buy_then_sell(self) -> None:
        """Buy and sell should update cash and positions correctly."""
        initial_cash = self.engine.cash

        # Buy 100 AAPL
        buy_order = self.engine.place_order("AAPL", "BUY", 100, "MARKET")
        self.assertEqual(buy_order["status"], "FILLED")
        buy_price = buy_order["fillPrice"]
        cash_after_buy = self.engine.cash

        self.assertEqual(self.engine.positions["AAPL"]["quantity"], 100)
        self.assertAlmostEqual(cash_after_buy, initial_cash - 100 * buy_price, delta=1.0)

        # Sell 100 AAPL
        sell_order = self.engine.place_order("AAPL", "SELL", 100, "MARKET")
        self.assertEqual(sell_order["status"], "FILLED")
        sell_price = sell_order["fillPrice"]
        cash_after_sell = self.engine.cash

        # Position should be closed
        self.assertNotIn("AAPL", self.engine.positions)

        # Cash should reflect sale proceeds (minus slippage)
        expected_proceeds = 100 * sell_price
        self.assertAlmostEqual(cash_after_sell, cash_after_buy + expected_proceeds, delta=1.0)

    def test_reject_unknown_symbol(self) -> None:
        """Order for unknown symbol should be rejected."""
        order = self.engine.place_order("UNKNOWN", "BUY", 100, "MARKET")

        self.assertEqual(order["status"], "REJECTED")
        self.assertIn("reason", order)

    def test_reject_invalid_quantity(self) -> None:
        """Order with invalid quantity should be rejected."""
        order = self.engine.place_order("AAPL", "BUY", 0, "MARKET")

        self.assertEqual(order["status"], "REJECTED")

    def test_reject_oversell(self) -> None:
        """Selling more than held should be rejected."""
        order = self.engine.place_order("AAPL", "SELL", 100, "MARKET")

        self.assertEqual(order["status"], "REJECTED")
        self.assertNotIn("AAPL", self.engine.positions)

    def test_reject_insufficient_cash(self) -> None:
        """Buying without sufficient cash should be rejected."""
        quote = self.market.quote("AAPL")
        ask_price = quote["ask"]
        huge_quantity = int(self.engine.cash / ask_price) + 1000

        order = self.engine.place_order("AAPL", "BUY", huge_quantity, "MARKET")

        self.assertEqual(order["status"], "REJECTED")

    def test_limit_order_working(self) -> None:
        """Limit order not immediately marketable should stay WORKING."""
        quote = self.market.quote("AAPL")
        ask_price = quote["ask"]

        # Place a limit BUY order well below the ask
        low_limit = ask_price * 0.80  # 20% below ask
        order = self.engine.place_order("AAPL", "BUY", 100, "LIMIT", price=low_limit)

        self.assertEqual(order["status"], "WORKING")
        self.assertNotIn("AAPL", self.engine.positions)

    def test_limit_order_immediate_fill(self) -> None:
        """Marketable limit order should fill immediately."""
        quote = self.market.quote("AAPL")
        ask_price = quote["ask"]

        # Place a limit BUY order well above the ask
        high_limit = ask_price * 1.20  # 20% above ask
        order = self.engine.place_order("AAPL", "BUY", 100, "LIMIT", price=high_limit)

        self.assertEqual(order["status"], "FILLED")
        self.assertIn("AAPL", self.engine.positions)

    def test_process_open_orders_no_fill(self) -> None:
        """process_open_orders should not fill unmarketable orders."""
        quote = self.market.quote("AAPL")
        ask_price = quote["ask"]

        # Place a limit BUY order well below ask (not immediately marketable)
        limit_price = ask_price * 0.70  # 30% below ask

        order = self.engine.place_order("AAPL", "BUY", 50, "LIMIT", price=limit_price)
        self.assertEqual(order["status"], "WORKING")
        order_id = order["orderId"]

        # Process orders - this order should remain WORKING
        self.engine.process_open_orders()

        # Check that order is still working
        updated_order = self.engine.get_order(order_id)
        self.assertEqual(updated_order["status"], "WORKING")

    def test_cancel_order(self) -> None:
        """Canceling a WORKING order should update status."""
        quote = self.market.quote("AAPL")
        ask_price = quote["ask"]

        # Place a limit BUY order not immediately marketable
        low_limit = ask_price * 0.50
        order = self.engine.place_order("AAPL", "BUY", 100, "LIMIT", price=low_limit)
        order_id = order["orderId"]

        self.assertEqual(order["status"], "WORKING")

        # Cancel it
        cancel_result = self.engine.cancel_order(order_id)

        self.assertEqual(cancel_result.get("status"), "CANCELLED")

        # Check that order status is updated
        cancelled_order = self.engine.get_order(order_id)
        self.assertEqual(cancelled_order["status"], "CANCELLED")

    def test_cancel_releases_reserved_cash(self) -> None:
        """Canceling a BUY LIMIT should release reserved cash."""
        quote = self.market.quote("AAPL")
        ask_price = quote["ask"]
        initial_cash = self.engine.cash

        # Place a limit BUY order
        limit_price = ask_price * 0.70
        order = self.engine.place_order("AAPL", "BUY", 100, "LIMIT", price=limit_price)
        order_id = order["orderId"]

        reserved_after_order = self.engine.reserved_cash
        self.assertGreater(reserved_after_order, 0)

        # Cancel it
        self.engine.cancel_order(order_id)

        # Reserved cash should be released
        self.assertEqual(self.engine.reserved_cash, 0.0)
        self.assertEqual(self.engine.cash, initial_cash)

    def test_cancel_filled_order_fails(self) -> None:
        """Canceling a FILLED order should fail."""
        order = self.engine.place_order("AAPL", "BUY", 100, "MARKET")
        order_id = order["orderId"]

        self.assertEqual(order["status"], "FILLED")

        # Try to cancel
        cancel_result = self.engine.cancel_order(order_id)

        self.assertIn("error", cancel_result)

    def test_get_order(self) -> None:
        """get_order should return order details."""
        order = self.engine.place_order("AAPL", "BUY", 100, "MARKET")
        order_id = order["orderId"]

        retrieved = self.engine.get_order(order_id)

        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["orderId"], order_id)
        self.assertEqual(retrieved["symbol"], "AAPL")
        self.assertEqual(retrieved["quantity"], 100)

    def test_list_orders(self) -> None:
        """list_orders should return all orders."""
        self.engine.place_order("AAPL", "BUY", 100, "MARKET")
        self.engine.place_order("MSFT", "BUY", 50, "MARKET")

        orders = self.engine.list_orders()

        self.assertEqual(len(orders), 2)

    def test_snapshot_structure(self) -> None:
        """Snapshot should have correct structure."""
        self.engine.place_order("AAPL", "BUY", 100, "MARKET")

        snapshot = self.engine.snapshot()

        self.assertIn("accountId", snapshot)
        self.assertIn("cash", snapshot)
        self.assertIn("positions", snapshot)
        self.assertIn("equity", snapshot)
        self.assertIn("realizedPnl", snapshot)
        self.assertIn("timestamp", snapshot)

        self.assertEqual(snapshot["accountId"], "TEST-001")
        self.assertGreater(len(snapshot["positions"]), 0)

    def test_snapshot_equity_calculation(self) -> None:
        """Snapshot equity should equal cash + market value of positions."""
        initial_cash = 100_000.0

        # Buy AAPL
        self.engine.place_order("AAPL", "BUY", 100, "MARKET")

        snapshot = self.engine.snapshot()

        # Equity = cash + sum of position market values
        position_market_value = sum(pos["marketValue"] for pos in snapshot["positions"])
        calculated_equity = snapshot["cash"] + position_market_value

        self.assertAlmostEqual(snapshot["equity"], calculated_equity, places=2)

    def test_multiple_positions(self) -> None:
        """Account should handle multiple positions."""
        self.engine.place_order("AAPL", "BUY", 100, "MARKET")
        self.engine.place_order("MSFT", "BUY", 50, "MARKET")
        self.engine.place_order("NVDA", "BUY", 30, "MARKET")

        snapshot = self.engine.snapshot()

        self.assertEqual(len(snapshot["positions"]), 3)
        symbols = {pos["symbol"] for pos in snapshot["positions"]}
        self.assertEqual(symbols, {"AAPL", "MSFT", "NVDA"})

    def test_position_average_price(self) -> None:
        """Position should track average price correctly."""
        # Buy 100 shares at first ask
        order1 = self.engine.place_order("AAPL", "BUY", 100, "MARKET")
        fill_price1 = order1["fillPrice"]

        # Verify average price
        self.assertAlmostEqual(
            self.engine.positions["AAPL"]["averagePrice"], fill_price1, places=2
        )

    def test_order_dict_structure(self) -> None:
        """Order dicts should have correct structure."""
        order = self.engine.place_order("AAPL", "BUY", 100, "MARKET")

        required_keys = [
            "orderId",
            "symbol",
            "instruction",
            "quantity",
            "orderType",
            "limitPrice",
            "status",
            "fillPrice",
            "placedAt",
            "filledAt",
        ]

        for key in required_keys:
            self.assertIn(key, order)


class TestAccountEngineRealisticScenario(unittest.TestCase):
    """Tests for realistic trading scenarios."""

    def setUp(self) -> None:
        """Set up test fixtures."""
        self.market = MarketSim(seed=123, symbols=["AAPL", "MSFT", "TSLA"])
        self.engine = AccountEngine(self.market, account_id="REAL-001", starting_cash=50_000.0)

    def test_portfolio_building(self) -> None:
        """Test building a diversified portfolio."""
        initial_cash = self.engine.cash

        # Build a portfolio
        self.engine.place_order("AAPL", "BUY", 50, "MARKET")
        self.engine.place_order("MSFT", "BUY", 30, "MARKET")
        self.engine.place_order("TSLA", "BUY", 20, "MARKET")

        snapshot = self.engine.snapshot()

        # Should have 3 positions
        self.assertEqual(len(snapshot["positions"]), 3)

        # Cash should be less than initial
        self.assertLess(snapshot["cash"], initial_cash)

        # Equity should be less than initial (slippage)
        self.assertLess(snapshot["equity"], initial_cash)

    def test_profit_calculation(self) -> None:
        """Test that profit/loss is calculated."""
        # Buy AAPL
        buy_order = self.engine.place_order("AAPL", "BUY", 100, "MARKET")
        buy_price = buy_order["fillPrice"]

        # Force a price change by advancing time in market
        # Create a new market at a different point in time
        new_market = MarketSim(seed=42, symbols=["AAPL", "MSFT", "NVDA"], time_scale=1000000)
        time.sleep(0.01)  # Advance 10000 simulated seconds

        self.engine.market = new_market

        # Get new quote
        new_quote = new_market.quote("AAPL")
        new_price = new_quote["last"]

        # Sell and check P&L
        sell_order = self.engine.place_order("AAPL", "SELL", 100, "MARKET")

        # Realized P&L should reflect the price difference (minus slippage)
        self.assertNotEqual(self.engine.realized_pnl, 0.0)


if __name__ == "__main__":
    unittest.main()
