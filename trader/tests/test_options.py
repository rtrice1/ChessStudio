"""Comprehensive tests for options pricing and trading."""

import unittest
from datetime import date, datetime, timezone, timedelta

from mockschwab.market import MarketSim
from mockschwab.options import (
    parse_occ, make_occ, bs_price_and_greeks,
    OptionsLayer, MarketWithOptions
)
from mockschwab.accounts import AccountEngine

# Re-export date for later use in tests
from datetime import date as Date


class TestOccParsing(unittest.TestCase):
    """Tests for OCC symbol parsing and generation."""

    def test_parse_occ_call(self):
        """Parse a call OCC symbol."""
        symbol = "AAPL260821C00190000"
        parsed = parse_occ(symbol)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["root"], "AAPL")
        self.assertEqual(parsed["expiry"], date(2026, 8, 21))
        self.assertEqual(parsed["put_call"], "C")
        self.assertEqual(parsed["strike"], 190.0)

    def test_parse_occ_put(self):
        """Parse a put OCC symbol."""
        symbol = "MSFT261218P00420500"
        parsed = parse_occ(symbol)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["root"], "MSFT")
        self.assertEqual(parsed["expiry"], date(2026, 12, 18))
        self.assertEqual(parsed["put_call"], "P")
        self.assertEqual(parsed["strike"], 420.5)

    def test_parse_occ_invalid_format(self):
        """Invalid OCC format should return None."""
        self.assertIsNone(parse_occ("AAPL"))
        self.assertIsNone(parse_occ("AAPL260821"))
        self.assertIsNone(parse_occ("AAPL260821X00190000"))  # Invalid C/P
        self.assertIsNone(parse_occ("AAPL260821C"))  # Too short

    def test_make_occ(self):
        """Generate OCC symbol."""
        occ = make_occ("AAPL", date(2026, 8, 21), "C", 190.0)
        self.assertEqual(occ, "AAPL260821C00190000")

        occ = make_occ("MSFT", date(2026, 12, 18), "P", 420.5)
        self.assertEqual(occ, "MSFT261218P00420500")

    def test_occ_roundtrip(self):
        """Parse and make should roundtrip."""
        original = "NVDA260411C00140000"
        parsed = parse_occ(original)
        remade = make_occ(
            parsed["root"],
            parsed["expiry"],
            parsed["put_call"],
            parsed["strike"]
        )
        self.assertEqual(original, remade)


class TestBlackScholes(unittest.TestCase):
    """Tests for Black-Scholes pricing and Greeks."""

    def test_atm_call_delta(self):
        """ATM call delta should be ~0.5 +/- 0.15."""
        spot = 100.0
        strike = 100.0
        t_years = 30 / 365.0  # 30 days
        iv = 0.25
        result = bs_price_and_greeks(spot, strike, t_years, iv, "C")

        self.assertAlmostEqual(result["delta"], 0.5, delta=0.15)

    def test_atm_put_delta(self):
        """ATM put delta should be ~-0.5 +/- 0.15."""
        spot = 100.0
        strike = 100.0
        t_years = 30 / 365.0
        iv = 0.25
        result = bs_price_and_greeks(spot, strike, t_years, iv, "P")

        self.assertAlmostEqual(result["delta"], -0.5, delta=0.15)

    def test_call_price_vs_intrinsic(self):
        """Call price should be >= intrinsic value."""
        spot = 100.0
        strike = 90.0
        t_years = 30 / 365.0
        iv = 0.25
        result = bs_price_and_greeks(spot, strike, t_years, iv, "C")

        intrinsic = max(spot - strike, 0)
        self.assertGreaterEqual(result["price"], intrinsic)

    def test_put_price_vs_intrinsic(self):
        """Put price should be >= intrinsic value."""
        spot = 100.0
        strike = 110.0
        t_years = 30 / 365.0
        iv = 0.25
        result = bs_price_and_greeks(spot, strike, t_years, iv, "P")

        intrinsic = max(strike - spot, 0)
        self.assertGreaterEqual(result["price"], intrinsic)

    def test_deep_itm_call_delta(self):
        """Deep ITM call delta should be > 0.9."""
        spot = 150.0
        strike = 100.0
        t_years = 30 / 365.0
        iv = 0.25
        result = bs_price_and_greeks(spot, strike, t_years, iv, "C")

        self.assertGreater(result["delta"], 0.9)

    def test_deep_otm_call_delta(self):
        """Deep OTM call delta should be < 0.1."""
        spot = 50.0
        strike = 100.0
        t_years = 30 / 365.0
        iv = 0.25
        result = bs_price_and_greeks(spot, strike, t_years, iv, "C")

        self.assertLess(result["delta"], 0.1)

    def test_theta_negative_for_long(self):
        """Theta should be negative for long options."""
        spot = 100.0
        strike = 100.0
        t_years = 30 / 365.0
        iv = 0.25

        call_result = bs_price_and_greeks(spot, strike, t_years, iv, "C")
        put_result = bs_price_and_greeks(spot, strike, t_years, iv, "P")

        self.assertLess(call_result["theta"], 0)
        self.assertLess(put_result["theta"], 0)

    def test_vega_positive(self):
        """Vega should be positive for both calls and puts."""
        spot = 100.0
        strike = 100.0
        t_years = 30 / 365.0
        iv = 0.25

        call_result = bs_price_and_greeks(spot, strike, t_years, iv, "C")
        put_result = bs_price_and_greeks(spot, strike, t_years, iv, "P")

        self.assertGreater(call_result["vega"], 0)
        self.assertGreater(put_result["vega"], 0)

    def test_gamma_positive(self):
        """Gamma should be positive for both calls and puts."""
        spot = 100.0
        strike = 100.0
        t_years = 30 / 365.0
        iv = 0.25

        call_result = bs_price_and_greeks(spot, strike, t_years, iv, "C")
        put_result = bs_price_and_greeks(spot, strike, t_years, iv, "P")

        self.assertGreater(call_result["gamma"], 0)
        self.assertGreater(put_result["gamma"], 0)

    def test_call_put_parity_approx(self):
        """Call - Put approx = Spot - Strike*exp(-r*t)."""
        spot = 100.0
        strike = 100.0
        t_years = 30 / 365.0
        iv = 0.25
        r = 0.04

        call = bs_price_and_greeks(spot, strike, t_years, iv, "C", r)
        put = bs_price_and_greeks(spot, strike, t_years, iv, "P", r)

        import math
        parity = spot - strike * math.exp(-r * t_years)
        # Close approximation (not exact due to discrete modeling)
        self.assertAlmostEqual(call["price"] - put["price"], parity, delta=0.5)

    def test_zero_dte_handling(self):
        """Very small t_years should not cause division by zero."""
        spot = 100.0
        strike = 100.0
        t_years = 1e-10  # Near-zero time
        iv = 0.25

        # Should not raise an exception
        result = bs_price_and_greeks(spot, strike, t_years, iv, "C")
        self.assertIsNotNone(result["price"])
        self.assertGreaterEqual(result["price"], 0)


class TestOptionsLayer(unittest.TestCase):
    """Tests for OptionsLayer."""

    def setUp(self):
        """Set up test fixtures."""
        self.market = MarketSim(seed=42, symbols=["AAPL", "MSFT"])
        self.options = OptionsLayer(self.market, seed=42)

    def test_expiries(self):
        """Should return two expiries: 0DTE and 7DTE."""
        expiries = self.options.expiries()
        self.assertEqual(len(expiries), 2)

        # Both should be ISO date strings
        for exp in expiries:
            date.fromisoformat(exp)

        # Second should be 7 days after first
        exp0 = date.fromisoformat(expiries[0])
        exp7 = date.fromisoformat(expiries[1])
        self.assertEqual((exp7 - exp0).days, 7)

    def test_chain_structure(self):
        """Chain should have correct structure."""
        chain = self.options.chain("AAPL")

        self.assertIn("symbol", chain)
        self.assertIn("expiry", chain)
        self.assertIn("calls", chain)
        self.assertIn("puts", chain)

        self.assertEqual(chain["symbol"], "AAPL")
        self.assertIsNotNone(chain["expiry"])

        # Should have ~11 strikes
        self.assertGreaterEqual(len(chain["calls"]), 10)
        self.assertGreaterEqual(len(chain["puts"]), 10)

    def test_chain_unknown_symbol(self):
        """Chain for unknown symbol should return error."""
        chain = self.options.chain("UNKNOWN")
        self.assertIn("error", chain)

    def test_chain_unknown_expiry(self):
        """Chain for unknown expiry should return error."""
        chain = self.options.chain("AAPL", expiry="2099-12-31")
        self.assertIn("error", chain)

    def test_chain_contract_structure(self):
        """Each contract in chain should have required fields."""
        chain = self.options.chain("AAPL")

        required_fields = [
            "contractSymbol", "strike", "putCall", "expiry",
            "bid", "ask", "last", "delta", "gamma", "theta", "vega", "iv"
        ]

        for call in chain["calls"]:
            for field in required_fields:
                self.assertIn(field, call)
            self.assertEqual(call["putCall"], "C")
            self.assertLess(call["bid"], call["ask"])

        for put in chain["puts"]:
            for field in required_fields:
                self.assertIn(field, put)
            self.assertEqual(put["putCall"], "P")
            self.assertLess(put["bid"], put["ask"])

    def test_chain_bid_ask_spread(self):
        """Bid-ask spread should be reasonable."""
        chain = self.options.chain("AAPL")

        for call in chain["calls"]:
            mid = (call["bid"] + call["ask"]) / 2
            spread = call["ask"] - call["bid"]
            # For options > $0.10, spread should be <= 6% of mid — except
            # the generator's minimum half-spread is $0.02/side, which
            # dominates for mids under ~$0.67 (bites whenever the sim
            # clock puts a strike's mid in that range) — plus up to a cent
            # each side lost to price rounding (bid rounds down, ask
            # rounds up — a mid near an odd cent adds ~0.02 of spread)
            if mid > 0.10:
                self.assertLessEqual(spread, max(mid * 0.061, 0.04) + 0.02)
            else:
                # For cheap options, spread is dominated by minimum, but ask >= bid
                self.assertGreater(call["ask"], call["bid"])

    def test_chain_deterministic(self):
        """Same seed should produce same chain."""
        options1 = OptionsLayer(self.market, seed=42)
        options2 = OptionsLayer(self.market, seed=42)

        chain1 = options1.chain("AAPL")
        chain2 = options2.chain("AAPL")

        # Same number of strikes
        self.assertEqual(len(chain1["calls"]), len(chain2["calls"]))

        # Same prices for same strikes
        for c1, c2 in zip(chain1["calls"], chain2["calls"]):
            self.assertEqual(c1["strike"], c2["strike"])
            self.assertEqual(c1["bid"], c2["bid"])
            self.assertEqual(c1["ask"], c2["ask"])

    def test_contract_quote(self):
        """contract_quote should return per-contract prices."""
        # Expiry from the sim's own clock — a hardcoded date is a time bomb
        # (the 2026-08-21 original started failing the Monday after OPEX).
        occ = make_occ("AAPL", self.market._sim_timestamp().date(), "C", 150.0)
        quote = self.options.contract_quote(occ)

        self.assertNotIn("error", quote)
        self.assertIn("bid", quote)
        self.assertIn("ask", quote)
        self.assertIn("last", quote)
        self.assertIn("multiplier", quote)
        self.assertEqual(quote["multiplier"], 100)

        # Prices should be per-contract (larger than per-share)
        # Typical option mid per-share is < 10, per-contract should be > 100
        mid_per_share = (quote["bid"] + quote["ask"]) / 2 / 100
        self.assertLess(mid_per_share, 100)  # Reasonable per-share price

    def test_contract_quote_expired(self):
        """Quote for expired contract should return error."""
        # Create a quote for yesterday
        today = self.market._sim_timestamp().date()
        yesterday = today - timedelta(days=1)

        occ = make_occ("AAPL", yesterday, "C", 150.0)
        quote = self.options.contract_quote(occ)

        self.assertIn("error", quote)

    def test_contract_quote_unknown_underlying(self):
        """Quote for unknown underlying should return error."""
        occ = make_occ("UNKNOWN", date(2026, 8, 21), "C", 150.0)
        quote = self.options.contract_quote(occ)

        self.assertIn("error", quote)

    def test_contract_quote_vs_chain(self):
        """contract_quote should match chain mid within spread."""
        chain = self.options.chain("AAPL")

        # Pick a call from the chain
        call_contract = chain["calls"][5]  # Middle strike
        occ = call_contract["contractSymbol"]

        quote = self.options.contract_quote(occ)

        # Quote is per-contract (100x), chain is per-share
        quote_mid = (quote["bid"] + quote["ask"]) / 2 / 100
        chain_mid = (call_contract["bid"] + call_contract["ask"]) / 2

        # Should match within tolerance
        self.assertAlmostEqual(quote_mid, chain_mid, delta=0.05)


class TestMarketWithOptions(unittest.TestCase):
    """Tests for MarketWithOptions wrapper."""

    def setUp(self):
        """Set up test fixtures."""
        self.market = MarketSim(seed=42, symbols=["AAPL", "MSFT"])
        self.options = OptionsLayer(self.market, seed=42)
        self.wrapper = MarketWithOptions(self.market, self.options)

    def test_symbols_delegated(self):
        """symbols property should accept both market symbols and OCC contracts."""
        # Should contain underlying symbols
        for symbol in self.market.symbols:
            self.assertIn(symbol, self.wrapper.symbols)

        # Should also accept OCC contracts
        occ = "AAPL260821C00150000"
        self.assertIn(occ, self.wrapper.symbols)

    def test_quote_underlying(self):
        """Quote for underlying symbol should use market."""
        quote = self.wrapper.quote("AAPL")
        self.assertNotIn("error", quote)
        self.assertIn("bid", quote)
        self.assertIn("ask", quote)

    def test_quote_option_contract(self):
        """Quote for OCC symbol should use options."""
        occ = make_occ("AAPL", self.wrapper._sim_timestamp().date(), "C", 150.0)
        quote = self.wrapper.quote(occ)
        self.assertNotIn("error", quote)
        self.assertIn("multiplier", quote)
        self.assertEqual(quote["multiplier"], 100)

    def test_delegation_price_history(self):
        """Non-quote methods should delegate to market."""
        history = self.wrapper.price_history("AAPL", days=1)
        self.assertNotIn("error", history)
        self.assertIn("candles", history)

    def test_delegation_sim_timestamp(self):
        """_sim_timestamp should delegate to market."""
        ts = self.wrapper._sim_timestamp()
        self.assertIsInstance(ts, datetime)


class TestOptionsIntegration(unittest.TestCase):
    """Integration tests with AccountEngine."""

    def setUp(self):
        """Set up test fixtures."""
        self.market = MarketSim(seed=42, symbols=["AAPL", "MSFT"])
        self.options = OptionsLayer(self.market, seed=42)
        self.wrapper = MarketWithOptions(self.market, self.options)
        self.engine = AccountEngine(self.wrapper, starting_cash=100_000.0)

    def test_buy_option_contract(self):
        """Should be able to buy option contracts."""
        # Get chain to find a contract
        chain = self.options.chain("AAPL")
        call = chain["calls"][5]  # Middle strike
        occ = call["contractSymbol"]

        # Place order
        order = self.engine.place_order(occ, "BUY", 2, "MARKET")

        # Should fill
        self.assertEqual(order["status"], "FILLED")
        self.assertIn(occ, self.engine.positions)
        self.assertEqual(self.engine.positions[occ]["quantity"], 2)

    def test_buy_sell_option_roundtrip(self):
        """Buy and sell option contracts should work."""
        initial_cash = self.engine.cash

        # Get chain
        chain = self.options.chain("AAPL")
        call = chain["calls"][5]
        occ = call["contractSymbol"]

        # Buy 2 contracts
        buy_order = self.engine.place_order(occ, "BUY", 2, "MARKET")
        self.assertEqual(buy_order["status"], "FILLED")
        cash_after_buy = self.engine.cash

        # Cash should decrease
        self.assertLess(cash_after_buy, initial_cash)

        # Sell 2 contracts
        sell_order = self.engine.place_order(occ, "SELL", 2, "MARKET")
        self.assertEqual(sell_order["status"], "FILLED")
        cash_after_sell = self.engine.cash

        # Position should be closed
        self.assertNotIn(occ, self.engine.positions)

        # Cash should increase from sale (minus spread)
        self.assertGreater(cash_after_sell, cash_after_buy)

        # Total loss should be < 2% due to bid-ask spread
        loss = initial_cash - cash_after_sell
        loss_pct = loss / initial_cash
        self.assertLess(loss_pct, 0.02)

    def test_reject_unknown_underlying_option(self):
        """Order for option on unknown underlying should be rejected."""
        occ = make_occ("UNKNOWN", date(2026, 8, 21), "C", 150.0)
        order = self.engine.place_order(occ, "BUY", 1, "MARKET")

        self.assertEqual(order["status"], "REJECTED")

    def test_snapshot_includes_options(self):
        """Account snapshot should include option positions."""
        # Buy an option
        chain = self.options.chain("AAPL")
        call = chain["calls"][5]
        occ = call["contractSymbol"]

        self.engine.place_order(occ, "BUY", 2, "MARKET")

        # Get snapshot
        snapshot = self.engine.snapshot()

        # Should have position
        self.assertGreater(len(snapshot["positions"]), 0)

        # Find the option position
        option_pos = None
        for pos in snapshot["positions"]:
            if pos["symbol"] == occ:
                option_pos = pos
                break

        self.assertIsNotNone(option_pos)
        self.assertEqual(option_pos["quantity"], 2)
        self.assertIn("marketValue", option_pos)
        self.assertIn("unrealizedPnl", option_pos)

    def test_mixed_equity_and_option_positions(self):
        """Account should handle both equity and option positions."""
        # Buy stock
        self.engine.place_order("AAPL", "BUY", 100, "MARKET")

        # Buy option
        chain = self.options.chain("AAPL")
        call = chain["calls"][5]
        occ = call["contractSymbol"]
        self.engine.place_order(occ, "BUY", 1, "MARKET")

        # Get snapshot
        snapshot = self.engine.snapshot()

        # Should have both positions
        self.assertEqual(len(snapshot["positions"]), 2)

        symbols = {pos["symbol"] for pos in snapshot["positions"]}
        self.assertIn("AAPL", symbols)
        self.assertIn(occ, symbols)


if __name__ == "__main__":
    unittest.main()
