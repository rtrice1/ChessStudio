"""Synthetic market simulator for paper trading."""

import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Optional


class MarketSim:
    """Synthetic market simulator using geometric Brownian motion."""

    def __init__(
        self,
        seed: int = 42,
        symbols: Optional[list[str]] = None,
        time_scale: float = 1.0,
    ) -> None:
        """
        Initialize market simulator.

        Args:
            seed: Random seed for deterministic pricing.
            symbols: List of symbols to track. Defaults to major US equities.
            time_scale: Multiplier for simulated time (1.0 = real-time, 60 = 1 sec = 1 min).
        """
        self.seed = seed
        self.rng = random.Random(seed)
        self.time_scale = time_scale
        self.start_time = time.time()  # Wall-clock reference
        self._start_sim_time = datetime.now(timezone.utc)

        # Default symbol universe with plausible starting prices
        default_symbols = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "SPY", "QQQ", "TSLA", "JPM", "XOM"]
        self.symbols = symbols or default_symbols

        # Starting prices (as of a reference date)
        self.start_prices = {
            "AAPL": 150.0,
            "MSFT": 380.0,
            "NVDA": 870.0,
            "GOOGL": 140.0,
            "AMZN": 180.0,
            "SPY": 450.0,
            "QQQ": 380.0,
            "TSLA": 220.0,
            "JPM": 190.0,
            "XOM": 105.0,
        }

        # Per-symbol parameters (drift, volatility, regime)
        self.params: dict[str, dict] = {}
        for symbol in self.symbols:
            self.rng.seed(hash(symbol) ^ seed)
            self.params[symbol] = {
                "drift": self.rng.gauss(0.0001, 0.00005),
                "volatility": self.rng.uniform(0.15, 0.35),
                "regime": self.rng.choice(["low_vol_bull", "low_vol_bear", "high_vol_bull", "high_vol_bear"]),
                "regime_switch_days": self.rng.randint(5, 20),
                "regime_counter": 0,
            }

        # Historical price cache: {symbol: {timestamp_key: price}}
        self._price_cache: dict[str, dict[float, float]] = {s: {} for s in self.symbols}

        # Reset RNG to seed for quote generation
        self.rng.seed(seed)

    def _get_sim_time_seconds(self) -> float:
        """Get simulated elapsed time in seconds since start."""
        wall_elapsed = time.time() - self.start_time
        return wall_elapsed * self.time_scale

    def _sim_timestamp(self) -> datetime:
        """Get current simulated timestamp."""
        sim_seconds = self._get_sim_time_seconds()
        return self._start_sim_time + timedelta(seconds=sim_seconds)

    def _update_regime(self, symbol: str, days_elapsed: float) -> None:
        """Update regime if threshold is crossed."""
        params = self.params[symbol]
        params["regime_counter"] += days_elapsed
        if params["regime_counter"] > params["regime_switch_days"]:
            # Switch regime
            regimes = ["low_vol_bull", "low_vol_bear", "high_vol_bull", "high_vol_bear"]
            current = params["regime"]
            regimes.remove(current)
            params["regime"] = self.rng.choice(regimes)
            params["regime_counter"] = 0
            params["regime_switch_days"] = self.rng.randint(5, 20)

    def _apply_regime(self, symbol: str) -> tuple[float, float]:
        """Get adjusted drift and volatility based on current regime."""
        params = self.params[symbol]
        regime = params["regime"]
        base_drift = params["drift"]
        base_vol = params["volatility"]

        if "bull" in regime:
            drift = base_drift + 0.0003
        else:
            drift = base_drift - 0.0003

        if "high_vol" in regime:
            volatility = base_vol * 1.5
        else:
            volatility = base_vol * 0.7

        return drift, volatility

    def _gbm_price(self, symbol: str, days_from_start: float) -> float:
        """
        Compute price at a given day offset using seeded GBM.
        Deterministic: same seed always produces same price for same symbol/day.
        """
        # Use a deterministic RNG stream for this symbol's price path
        path_rng = random.Random(hash(symbol) ^ self.seed)

        current_price = self.start_prices.get(symbol, 100.0)
        current_day = 0.0
        sim_regime_counter = 0
        current_regime_idx = hash(symbol) % 4  # Deterministic initial regime

        regimes = ["low_vol_bull", "low_vol_bear", "high_vol_bull", "high_vol_bear"]
        current_regime = regimes[current_regime_idx]

        # Get base params
        params = self.params[symbol]
        base_drift = params["drift"]
        base_vol = params["volatility"]

        dt = 0.01  # Step size in days (roughly 14 min intervals)
        steps = int(days_from_start / dt)

        for _ in range(steps):
            # Check regime switch
            sim_regime_counter += dt
            if sim_regime_counter > path_rng.randint(5, 20):
                regimes_copy = regimes.copy()
                regimes_copy.remove(current_regime)
                current_regime = path_rng.choice(regimes_copy)
                sim_regime_counter = 0

            # Get adjusted drift and vol
            if "bull" in current_regime:
                adj_drift = base_drift + 0.0003
            else:
                adj_drift = base_drift - 0.0003

            if "high_vol" in current_regime:
                adj_vol = base_vol * 1.5
            else:
                adj_vol = base_vol * 0.7

            # GBM step
            dW = path_rng.gauss(0, math.sqrt(dt))
            current_price = current_price * math.exp((adj_drift - 0.5 * adj_vol**2) * dt + adj_vol * dW)
            current_day += dt

        return max(current_price, 0.01)  # Prevent zero/negative prices

    def quote(self, symbol: str) -> dict:
        """
        Get current quote for a symbol.

        Returns:
            {"symbol", "bid", "ask", "last", "timestamp"}
        """
        if symbol not in self.symbols:
            return {"error": f"Unknown symbol: {symbol}"}

        sim_days = self._get_sim_time_seconds() / 86400.0
        last_price = self._gbm_price(symbol, sim_days)

        # Bid-ask spread: 2-5 basis points
        spread_bps = self.rng.uniform(0.0002, 0.0005)
        half_spread = last_price * spread_bps / 2
        bid = last_price - half_spread
        ask = last_price + half_spread

        timestamp = self._sim_timestamp().isoformat()

        return {
            "symbol": symbol,
            "bid": round(bid, 2),
            "ask": round(ask, 2),
            "last": round(last_price, 2),
            "timestamp": timestamp,
        }

    def price_history(self, symbol: str, days: int = 5, interval_minutes: int = 60) -> dict:
        """
        Get historical OHLCV data.

        Args:
            symbol: Ticker symbol.
            days: Number of days of history.
            interval_minutes: Candle interval in minutes.

        Returns:
            {"symbol", "candles": [{"open","high","low","close","volume","datetime"}]}
        """
        if symbol not in self.symbols:
            return {"error": f"Unknown symbol: {symbol}"}

        sim_time_now = self._get_sim_time_seconds()
        sim_days_now = sim_time_now / 86400.0

        candles = []
        interval_days = interval_minutes / (24 * 60)

        # Generate candles backward from now
        for i in range(int(days * 24 * 60 / interval_minutes)):
            candle_end_days = sim_days_now - i * interval_days
            candle_start_days = max(0.0, candle_end_days - interval_days)

            # Sample multiple points within interval to get OHLC
            num_samples = 5
            prices = []
            for j in range(num_samples):
                frac = j / num_samples
                sample_days = candle_start_days + frac * (candle_end_days - candle_start_days)
                prices.append(self._gbm_price(symbol, sample_days))

            open_price = prices[0]
            close_price = prices[-1]
            high_price = max(prices)
            low_price = min(prices)
            volume = self.rng.randint(100000, 10000000)

            candle_time = self._start_sim_time + timedelta(seconds=candle_end_days * 86400)

            candles.append(
                {
                    "open": round(open_price, 2),
                    "high": round(high_price, 2),
                    "low": round(low_price, 2),
                    "close": round(close_price, 2),
                    "volume": volume,
                    "datetime": candle_time.isoformat(),
                }
            )

        candles.reverse()  # Oldest first

        return {
            "symbol": symbol,
            "candles": candles,
        }
