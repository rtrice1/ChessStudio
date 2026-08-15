"""Unit tests for trader agent."""

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from agent.client import BrokerClient, BrokerError
from agent.indicators import (
    adx,
    atr,
    bollinger,
    day_stats,
    ema,
    latest_day,
    macd,
    obv,
    opening_range,
    pct_change,
    relative_volume,
    roc,
    rsi,
    sma,
    stochastic,
    summarize,
    vwap,
)
from agent.ledger import Ledger
from agent.poller import compute_alerts, sentiment_score, summarize_news


class TestIndicators(unittest.TestCase):
    """Test technical indicators."""

    def test_sma_basic(self):
        """Test simple moving average."""
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = sma(closes, 3)
        self.assertAlmostEqual(result, 4.0)  # (3 + 4 + 5) / 3

    def test_sma_not_enough_data(self):
        """Test SMA with insufficient data."""
        closes = [1.0, 2.0]
        result = sma(closes, 5)
        self.assertIsNone(result)

    def test_sma_single_value(self):
        """Test SMA at minimum."""
        closes = [1.0, 2.0, 3.0]
        result = sma(closes, 1)
        self.assertAlmostEqual(result, 3.0)

    def test_ema_basic(self):
        """Test exponential moving average."""
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = ema(closes, 3)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)

    def test_ema_not_enough_data(self):
        """Test EMA with insufficient data."""
        closes = [1.0, 2.0]
        result = ema(closes, 5)
        self.assertIsNone(result)

    def test_rsi_bounds(self):
        """Test RSI is between 0 and 100."""
        closes = list(range(1, 20))
        result = rsi(closes, 14)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result, 0)
        self.assertLessEqual(result, 100)

    def test_rsi_not_enough_data(self):
        """Test RSI with insufficient data."""
        closes = [1.0, 2.0, 3.0]
        result = rsi(closes, 14)
        self.assertIsNone(result)

    def test_rsi_uptrend(self):
        """Test RSI high in strong uptrend."""
        closes = [float(i) for i in range(1, 50)]
        result = rsi(closes, 14)
        self.assertIsNotNone(result)
        self.assertGreater(result, 50)

    def test_rsi_downtrend(self):
        """Test RSI low in strong downtrend."""
        closes = [float(50 - i) for i in range(1, 50)]
        result = rsi(closes, 14)
        self.assertIsNotNone(result)
        self.assertLess(result, 50)

    def test_atr_basic(self):
        """Test Average True Range."""
        candles = [
            {"high": 10.0, "low": 9.0, "close": 9.5},
            {"high": 11.0, "low": 9.5, "close": 10.5},
            {"high": 12.0, "low": 10.0, "close": 11.5},
        ]
        for i in range(1, 16):
            candles.append(
                {"high": 12.0 + i * 0.1, "low": 10.0 + i * 0.1, "close": 11.5 + i * 0.1}
            )
        result = atr(candles, 14)
        self.assertIsNotNone(result)
        self.assertGreater(result, 0)

    def test_atr_not_enough_data(self):
        """Test ATR with insufficient data."""
        candles = [{"high": 10.0, "low": 9.0, "close": 9.5}]
        result = atr(candles, 14)
        self.assertIsNone(result)

    def test_pct_change_basic(self):
        """Test percentage change."""
        closes = [100.0, 105.0, 110.0]
        result = pct_change(closes, 1)
        self.assertAlmostEqual(result, 5.0 / 105.0)  # (110 - 105) / 105 ≈ 0.0476

    def test_pct_change_not_enough_data(self):
        """Test pct_change with insufficient data."""
        closes = [100.0]
        result = pct_change(closes, 5)
        self.assertIsNone(result)

    def test_pct_change_zero_base(self):
        """Test pct_change with zero base."""
        closes = [0.0, 100.0, 150.0]
        result = pct_change(closes, 2)
        self.assertIsNone(result)

    def test_summarize_empty(self):
        """Test summarize with empty candles."""
        result = summarize([])
        self.assertIsNone(result["last_close"])
        self.assertIsNone(result["sma20"])
        self.assertIsNone(result["ema9"])
        self.assertIsNone(result["rsi14"])
        self.assertIsNone(result["atr14"])

    def test_summarize_short_data(self):
        """Test summarize with short data."""
        candles = [{"close": 100.0, "high": 101.0, "low": 99.0, "volume": 1000}]
        result = summarize(candles)
        self.assertAlmostEqual(result["last_close"], 100.0)
        self.assertIsNone(result["sma20"])
        self.assertIsNone(result["ema9"])

    def test_summarize_full(self):
        """Test summarize with sufficient data."""
        candles = [
            {"close": float(100 + i), "high": float(101 + i), "low": float(99 + i), "volume": 1000}
            for i in range(50)
        ]
        result = summarize(candles)
        self.assertIsNotNone(result["last_close"])
        self.assertIsNotNone(result["sma20"])
        self.assertIsNotNone(result["ema9"])
        self.assertIsNotNone(result["rsi14"])
        self.assertIsNotNone(result["atr14"])


class TestIntradayIndicators(unittest.TestCase):
    """Test intraday indicators: vwap, latest_day, opening_range, day_stats."""

    def test_vwap_basic(self):
        """Test VWAP hand-computed on 2 candles."""
        candles = [
            {"high": 100.0, "low": 99.0, "close": 99.5, "volume": 1000},
            {"high": 101.0, "low": 100.0, "close": 100.5, "volume": 2000},
        ]
        # typical_price_1 = (100 + 99 + 99.5) / 3 = 298.5 / 3 = 99.5
        # typical_price_2 = (101 + 100 + 100.5) / 3 = 301.5 / 3 = 100.5
        # vwap = (99.5 * 1000 + 100.5 * 2000) / (1000 + 2000)
        #      = (99500 + 201000) / 3000
        #      = 300500 / 3000 = 100.16666...
        result = vwap(candles)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 100.16666666, places=5)

    def test_vwap_three_candles(self):
        """Test VWAP with three candles."""
        candles = [
            {"high": 100.0, "low": 100.0, "close": 100.0, "volume": 100},
            {"high": 110.0, "low": 110.0, "close": 110.0, "volume": 200},
            {"high": 120.0, "low": 120.0, "close": 120.0, "volume": 300},
        ]
        # typical prices all equal close: 100, 110, 120
        # vwap = (100*100 + 110*200 + 120*300) / (100 + 200 + 300)
        #      = (10000 + 22000 + 36000) / 600
        #      = 68000 / 600 = 113.333...
        result = vwap(candles)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 113.333333, places=5)

    def test_vwap_empty(self):
        """Test VWAP on empty list."""
        result = vwap([])
        self.assertIsNone(result)

    def test_vwap_zero_volume(self):
        """Test VWAP with zero total volume."""
        candles = [
            {"high": 100.0, "low": 99.0, "close": 99.5, "volume": 0},
            {"high": 101.0, "low": 100.0, "close": 100.5, "volume": 0},
        ]
        result = vwap(candles)
        self.assertIsNone(result)

    def test_latest_day_single_date(self):
        """Test latest_day with all candles on same date."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "close": 100.0},
            {"datetime": "2024-01-15T10:00:00Z", "close": 101.0},
            {"datetime": "2024-01-15T10:30:00Z", "close": 102.0},
        ]
        result = latest_day(candles)
        self.assertEqual(len(result), 3)
        self.assertEqual(result, candles)

    def test_latest_day_multiple_dates(self):
        """Test latest_day splits two dates correctly."""
        candles = [
            {"datetime": "2024-01-14T15:00:00Z", "close": 100.0},
            {"datetime": "2024-01-14T15:30:00Z", "close": 100.5},
            {"datetime": "2024-01-15T09:30:00Z", "close": 101.0},
            {"datetime": "2024-01-15T10:00:00Z", "close": 101.5},
            {"datetime": "2024-01-15T10:30:00Z", "close": 102.0},
        ]
        result = latest_day(candles)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["datetime"], "2024-01-15T09:30:00Z")
        self.assertEqual(result[-1]["datetime"], "2024-01-15T10:30:00Z")

    def test_latest_day_empty(self):
        """Test latest_day with empty list."""
        result = latest_day([])
        self.assertEqual(result, [])

    def test_latest_day_no_datetime(self):
        """Test latest_day when candles lack datetime."""
        candles = [{"close": 100.0}, {"close": 101.0}]
        result = latest_day(candles)
        self.assertEqual(result, [])

    def test_opening_range_basic(self):
        """Test opening_range picks first-N-bars high/low of latest day."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "high": 100.0, "low": 99.0},
            {"datetime": "2024-01-15T10:00:00Z", "high": 101.0, "low": 99.5},
            {"datetime": "2024-01-15T10:30:00Z", "high": 102.5, "low": 100.0},
            {"datetime": "2024-01-15T11:00:00Z", "high": 105.0, "low": 98.0},
            {"datetime": "2024-01-15T11:30:00Z", "high": 110.0, "low": 95.0},
            {"datetime": "2024-01-15T12:00:00Z", "high": 112.0, "low": 100.0},
            {"datetime": "2024-01-15T12:30:00Z", "high": 115.0, "low": 111.0},
        ]
        result = opening_range(candles, bars=6)
        self.assertIsNotNone(result)
        # First 6 bars: highs = [100, 101, 102.5, 105, 110, 112], lows = [99, 99.5, 100, 98, 95, 100]
        self.assertEqual(result["high"], 112.0)
        self.assertEqual(result["low"], 95.0)

    def test_opening_range_default_bars(self):
        """Test opening_range with default bars=6."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "high": 100.0, "low": 99.0},
            {"datetime": "2024-01-15T10:00:00Z", "high": 101.0, "low": 99.5},
            {"datetime": "2024-01-15T10:30:00Z", "high": 102.5, "low": 100.0},
            {"datetime": "2024-01-15T11:00:00Z", "high": 105.0, "low": 98.0},
            {"datetime": "2024-01-15T11:30:00Z", "high": 110.0, "low": 95.0},
            {"datetime": "2024-01-15T12:00:00Z", "high": 112.0, "low": 100.0},
        ]
        result = opening_range(candles)
        self.assertIsNotNone(result)
        self.assertEqual(result["high"], 112.0)
        self.assertEqual(result["low"], 95.0)

    def test_opening_range_empty_latest_day(self):
        """Test opening_range when latest day has no candles."""
        result = opening_range([])
        self.assertIsNone(result)

    def test_opening_range_fewer_bars_than_requested(self):
        """Test opening_range when latest day has fewer bars than requested."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "high": 100.0, "low": 99.0},
            {"datetime": "2024-01-15T10:00:00Z", "high": 101.0, "low": 99.5},
            {"datetime": "2024-01-15T10:30:00Z", "high": 102.5, "low": 100.0},
        ]
        result = opening_range(candles, bars=6)
        self.assertIsNotNone(result)
        self.assertEqual(result["high"], 102.5)
        self.assertEqual(result["low"], 99.0)

    def test_day_stats_basic(self):
        """Test day_stats correctness."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000},
            {"datetime": "2024-01-15T10:00:00Z", "open": 101.0, "high": 105.0, "low": 100.0, "close": 103.0, "volume": 2000},
            {"datetime": "2024-01-15T10:30:00Z", "open": 103.0, "high": 104.0, "low": 102.0, "close": 103.5, "volume": 1500},
        ]
        result = day_stats(candles)
        self.assertIsNotNone(result)
        self.assertEqual(result["open"], 100.0)
        self.assertEqual(result["high"], 105.0)
        self.assertEqual(result["low"], 99.0)
        self.assertIsNotNone(result["vwap"])

    def test_day_stats_empty(self):
        """Test day_stats with empty list."""
        result = day_stats([])
        self.assertIsNone(result)

    def test_day_stats_with_latest_day(self):
        """Test day_stats uses only latest day."""
        candles = [
            {"datetime": "2024-01-14T15:00:00Z", "open": 90.0, "high": 95.0, "low": 88.0, "close": 91.0, "volume": 5000},
            {"datetime": "2024-01-15T09:30:00Z", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000},
            {"datetime": "2024-01-15T10:00:00Z", "open": 101.0, "high": 105.0, "low": 100.0, "close": 103.0, "volume": 2000},
        ]
        result = day_stats(candles)
        self.assertIsNotNone(result)
        # Should use only 2024-01-15 candles
        self.assertEqual(result["open"], 100.0)
        self.assertEqual(result["high"], 105.0)
        self.assertEqual(result["low"], 99.0)

    def test_summarize_new_keys_present(self):
        """Test summarize contains new keys."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000},
            {"datetime": "2024-01-15T10:00:00Z", "open": 101.0, "high": 105.0, "low": 100.0, "close": 103.0, "volume": 2000},
        ]
        result = summarize(candles)
        self.assertIn("vwap", result)
        self.assertIn("day_open", result)
        self.assertIn("day_high", result)
        self.assertIn("day_low", result)
        self.assertIn("range_high", result)
        self.assertIn("range_low", result)

    def test_summarize_new_keys_empty(self):
        """Test summarize with empty list returns None for new keys."""
        result = summarize([])
        self.assertIsNone(result["vwap"])
        self.assertIsNone(result["day_open"])
        self.assertIsNone(result["day_high"])
        self.assertIsNone(result["day_low"])
        self.assertIsNone(result["range_high"])
        self.assertIsNone(result["range_low"])

    def test_summarize_new_keys_short_list(self):
        """Test summarize tolerates short candle list."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0, "volume": 1000}
        ]
        result = summarize(candles)
        # Should have the keys even if some values are None
        self.assertIn("vwap", result)
        self.assertIn("day_open", result)
        self.assertIn("day_high", result)
        self.assertIn("day_low", result)
        self.assertIn("range_high", result)
        self.assertIn("range_low", result)
        # day_open should have the value since there's at least one candle
        self.assertEqual(result["day_open"], 100.0)

    def test_summarize_existing_keys_unchanged(self):
        """Test summarize keeps all existing keys."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "open": float(100 + i), "high": float(101 + i), "low": float(99 + i), "close": float(100 + i), "volume": 1000}
            for i in range(50)
        ]
        result = summarize(candles)
        self.assertIn("last_close", result)
        self.assertIn("sma20", result)
        self.assertIn("ema9", result)
        self.assertIn("rsi14", result)
        self.assertIn("atr14", result)
        self.assertIn("pct_change_1d", result)
        self.assertIn("pct_change_5d", result)


class TestMomentumIndicators(unittest.TestCase):
    """Test momentum indicators: MACD, Stochastic, ADX, ROC, Bollinger, OBV, Relative Volume."""

    def test_macd_not_enough_data(self):
        """Test MACD with insufficient data."""
        closes = [100.0, 101.0, 102.0]
        result = macd(closes)
        self.assertIsNone(result)

    def test_macd_accelerating_uptrend(self):
        """Test MACD histogram sign flips positive in accelerating uptrend."""
        # Slow rise then steep rise (need at least 35 closes for slow=26, signal=9)
        closes = [float(100 + i) for i in range(26)]  # 26 closes: slow rise
        closes.extend([float(126 + i * 5) for i in range(15)])  # steep rise

        result = macd(closes)
        self.assertIsNotNone(result)
        self.assertIn("macd", result)
        self.assertIn("signal", result)
        self.assertIn("hist", result)
        # In strong uptrend, hist should be positive
        self.assertGreater(result["hist"], 0)

    def test_macd_downtrend(self):
        """Test MACD in a downtrend."""
        closes = [float(200 - i) for i in range(26)]  # downtrend for 26 closes
        closes.extend([float(174 - i * 5) for i in range(15)])  # steep downtrend

        result = macd(closes)
        self.assertIsNotNone(result)
        # In strong downtrend, hist should be negative
        self.assertLess(result["hist"], 0)

    def test_stochastic_not_enough_data(self):
        """Test Stochastic with insufficient data."""
        candles = [{"high": 100.0, "low": 99.0, "close": 99.5}]
        result = stochastic(candles, k=14, d=3)
        self.assertIsNone(result)

    def test_stochastic_at_new_high(self):
        """Test Stochastic %K = 100 at new high."""
        candles = []
        for i in range(15):
            candles.append({
                "high": float(100 + i),
                "low": float(99 + i),
                "close": float(99.5 + i),
            })
        # Last candle at a new high relative to range
        candles.append({"high": 115.0, "low": 100.0, "close": 115.0})

        result = stochastic(candles, k=14, d=3)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["k"], 100.0)

    def test_stochastic_at_new_low(self):
        """Test Stochastic %K = 0 at new low."""
        candles = []
        for i in range(15):
            candles.append({
                "high": float(100 + i),
                "low": float(99 + i),
                "close": float(100 + i),
            })
        # Last candle at bottom of range
        candles.append({"high": 113.0, "low": 85.0, "close": 85.0})

        result = stochastic(candles, k=14, d=3)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["k"], 0.0)

    def test_stochastic_bounds(self):
        """Test Stochastic %K and %D are bounded [0, 100]."""
        candles = [
            {"high": 100.0 + i, "low": 99.0 + i, "close": 99.5 + i}
            for i in range(20)
        ]
        result = stochastic(candles, k=14, d=3)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["k"], 0.0)
        self.assertLessEqual(result["k"], 100.0)
        self.assertGreaterEqual(result["d"], 0.0)
        self.assertLessEqual(result["d"], 100.0)

    def test_adx_not_enough_data(self):
        """Test ADX with insufficient data."""
        candles = [{"high": 100.0, "low": 99.0, "close": 99.5}]
        result = adx(candles, n=14)
        self.assertIsNone(result)

    def test_adx_trending_vs_choppy(self):
        """Test ADX rises in persistent trend vs flat/oscillating series."""
        # Trending series: consistent uptrend
        trending = []
        for i in range(50):
            trending.append({
                "high": 100.0 + i * 2,
                "low": 99.0 + i * 2,
                "close": 99.5 + i * 2,
            })

        # Choppy series: oscillating
        choppy = []
        for i in range(50):
            if i % 2 == 0:
                choppy.append({"high": 105.0, "low": 100.0, "close": 105.0})
            else:
                choppy.append({"high": 110.0, "low": 105.0, "close": 105.0})

        trending_adx = adx(trending, n=14)
        choppy_adx = adx(choppy, n=14)

        self.assertIsNotNone(trending_adx)
        self.assertIsNotNone(choppy_adx)
        self.assertGreater(trending_adx["adx"], choppy_adx["adx"])
        self.assertGreaterEqual(trending_adx["adx"], 0)
        self.assertLessEqual(trending_adx["adx"], 100)
        self.assertGreaterEqual(choppy_adx["adx"], 0)
        self.assertLessEqual(choppy_adx["adx"], 100)

    def test_adx_di_uptrend(self):
        """Test +DI > -DI in uptrend."""
        candles = []
        for i in range(50):
            candles.append({
                "high": 100.0 + i * 2,
                "low": 99.0 + i * 2,
                "close": 99.5 + i * 2,
            })

        result = adx(candles, n=14)
        self.assertIsNotNone(result)
        self.assertGreater(result["plus_di"], result["minus_di"])

    def test_adx_di_downtrend(self):
        """Test -DI > +DI in downtrend."""
        candles = []
        for i in range(50):
            candles.append({
                "high": 200.0 - i * 2,
                "low": 199.0 - i * 2,
                "close": 199.5 - i * 2,
            })

        result = adx(candles, n=14)
        self.assertIsNotNone(result)
        self.assertGreater(result["minus_di"], result["plus_di"])

    def test_roc_not_enough_data(self):
        """Test ROC with insufficient data."""
        closes = [100.0, 101.0]
        result = roc(closes, n=10)
        self.assertIsNone(result)

    def test_roc_exact(self):
        """Test ROC exact calculation on known series."""
        closes = [100.0] * 10 + [110.0]  # 10% increase from 100 to 110
        result = roc(closes, n=10)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 10.0)  # 100 * (110/100 - 1) = 10

    def test_roc_decrease(self):
        """Test ROC with price decrease."""
        closes = [100.0] * 10 + [90.0]  # 10% decrease
        result = roc(closes, n=10)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, -10.0)  # 100 * (90/100 - 1) = -10

    def test_bollinger_not_enough_data(self):
        """Test Bollinger with insufficient data."""
        closes = [100.0, 101.0]
        result = bollinger(closes, n=20)
        self.assertIsNone(result)

    def test_bollinger_exact_constant(self):
        """Test Bollinger on constant series: mid = close, upper/lower = mid +/- 0."""
        closes = [100.0] * 20
        result = bollinger(closes, n=20, k=2.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["mid"], 100.0)
        self.assertAlmostEqual(result["upper"], 100.0)
        self.assertAlmostEqual(result["lower"], 100.0)
        self.assertAlmostEqual(result["bandwidth"], 0.0)

    def test_bollinger_exact_step(self):
        """Test Bollinger on constant-then-step series."""
        closes = [100.0] * 20 + [105.0]  # constant then step up
        result = bollinger(closes, n=20, k=2.0)
        self.assertIsNotNone(result)
        # Last 20: 19x100 + 1x105
        mid_expected = (19 * 100 + 105) / 20  # 100.25
        self.assertAlmostEqual(result["mid"], mid_expected, places=5)
        self.assertGreater(result["upper"], result["mid"])
        self.assertLess(result["lower"], result["mid"])

    def test_bollinger_percent_b_middle(self):
        """Test Bollinger %B at middle of band."""
        closes = [100.0] * 20 + [100.0]  # all same
        result = bollinger(closes, n=20, k=2.0)
        self.assertIsNotNone(result)
        # At middle of band
        self.assertAlmostEqual(result["percent_b"], 0.5)

    def test_bollinger_percent_b_at_upper(self):
        """Test Bollinger %B at upper band."""
        closes = [100.0] * 20
        result = bollinger(closes, n=20, k=2.0)
        self.assertIsNotNone(result)
        # At flat band, percent_b should be 0.5 (close is at both upper and lower)
        self.assertAlmostEqual(result["percent_b"], 0.5)

    def test_obv_not_enough_data(self):
        """Test OBV with insufficient data."""
        candles = [{"close": 100.0, "volume": 1000}]
        result = obv(candles)
        self.assertIsNone(result)

    def test_obv_exact_4_candle(self):
        """Test OBV exact on tiny 4-candle case."""
        candles = [
            {"close": 100.0, "volume": 100},  # initial
            {"close": 101.0, "volume": 200},  # up: +200
            {"close": 100.0, "volume": 150},  # down: -150
            {"close": 102.0, "volume": 300},  # up: +300
        ]
        result = obv(candles)
        self.assertIsNotNone(result)
        # OBV = 100 + 200 - 150 + 300 = 450
        self.assertAlmostEqual(result, 450.0)

    def test_obv_uptrend(self):
        """Test OBV in uptrend."""
        candles = [
            {"close": float(100 + i), "volume": 1000}
            for i in range(10)
        ]
        result = obv(candles)
        self.assertIsNotNone(result)
        # All closes are increasing, so volume accumulates
        self.assertGreater(result, 0)

    def test_relative_volume_not_enough_data(self):
        """Test Relative Volume with insufficient data."""
        candles = [{"volume": 1000}, {"volume": 2000}]
        result = relative_volume(candles, bars=6)
        self.assertIsNone(result)

    def test_relative_volume_doubled(self):
        """Test Relative Volume > 1 when recent volume doubled."""
        # 10 earlier candles with volume 100
        candles = [{"volume": 100} for _ in range(10)]
        # 6 recent candles with volume 200 (doubled)
        candles.extend([{"volume": 200} for _ in range(6)])

        result = relative_volume(candles, bars=6)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 2.0)

    def test_relative_volume_halved(self):
        """Test Relative Volume < 1 when recent volume halved."""
        candles = [{"volume": 100} for _ in range(10)]
        candles.extend([{"volume": 50} for _ in range(6)])

        result = relative_volume(candles, bars=6)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 0.5)

    def test_relative_volume_zero_earlier_mean(self):
        """Test Relative Volume returns None when earlier mean is 0."""
        candles = [{"volume": 0} for _ in range(10)]
        candles.extend([{"volume": 100} for _ in range(6)])

        result = relative_volume(candles, bars=6)
        self.assertIsNone(result)

    def test_summarize_momentum_keys_present(self):
        """Test summarize includes all momentum keys."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "open": 100.0 + i, "high": 101.0 + i, "low": 99.0 + i, "close": 100.0 + i, "volume": 1000}
            for i in range(50)
        ]
        result = summarize(candles)
        self.assertIn("macd_hist", result)
        self.assertIn("stoch_k", result)
        self.assertIn("stoch_d", result)
        self.assertIn("adx", result)
        self.assertIn("plus_di", result)
        self.assertIn("minus_di", result)
        self.assertIn("roc10", result)
        self.assertIn("bb_percent_b", result)
        self.assertIn("bb_bandwidth", result)
        self.assertIn("rel_volume", result)

    def test_summarize_momentum_keys_empty(self):
        """Test summarize with empty list has all momentum keys."""
        result = summarize([])
        self.assertIsNone(result["macd_hist"])
        self.assertIsNone(result["stoch_k"])
        self.assertIsNone(result["stoch_d"])
        self.assertIsNone(result["adx"])
        self.assertIsNone(result["plus_di"])
        self.assertIsNone(result["minus_di"])
        self.assertIsNone(result["roc10"])
        self.assertIsNone(result["bb_percent_b"])
        self.assertIsNone(result["bb_bandwidth"])
        self.assertIsNone(result["rel_volume"])

    def test_summarize_momentum_keys_short_list(self):
        """Test summarize tolerates short candle list."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000}
        ]
        result = summarize(candles)
        # Should have all keys even if values are None
        self.assertIn("macd_hist", result)
        self.assertIn("stoch_k", result)
        self.assertIn("stoch_d", result)
        self.assertIn("adx", result)
        self.assertIn("plus_di", result)
        self.assertIn("minus_di", result)
        self.assertIn("roc10", result)
        self.assertIn("bb_percent_b", result)
        self.assertIn("bb_bandwidth", result)
        self.assertIn("rel_volume", result)

    def test_summarize_preserves_existing_keys(self):
        """Test summarize keeps all existing keys."""
        candles = [
            {"datetime": "2024-01-15T09:30:00Z", "open": float(100 + i), "high": float(101 + i), "low": float(99 + i), "close": float(100 + i), "volume": 1000}
            for i in range(50)
        ]
        result = summarize(candles)
        # Existing keys should all be present
        self.assertIn("last_close", result)
        self.assertIn("sma20", result)
        self.assertIn("ema9", result)
        self.assertIn("rsi14", result)
        self.assertIn("atr14", result)
        self.assertIn("pct_change_1d", result)
        self.assertIn("pct_change_5d", result)
        self.assertIn("vwap", result)
        self.assertIn("day_open", result)
        self.assertIn("day_high", result)
        self.assertIn("day_low", result)
        self.assertIn("range_high", result)
        self.assertIn("range_low", result)


class TestLedger(unittest.TestCase):
    """Test ledger functionality."""

    def test_ledger_record_and_read(self):
        """Test recording and reading entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "trades.jsonl"
            ledger = Ledger(str(ledger_path))

            ledger.record("signal", {"symbol": "AAPL", "price": 150.0})
            ledger.record("fill", {"symbol": "AAPL", "instruction": "BUY", "quantity": 10})

            entries = ledger.entries()
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0]["kind"], "signal")
            self.assertEqual(entries[1]["kind"], "fill")

    def test_ledger_filter_by_kind(self):
        """Test filtering entries by kind."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "trades.jsonl"
            ledger = Ledger(str(ledger_path))

            ledger.record("signal", {"symbol": "AAPL"})
            ledger.record("fill", {"symbol": "AAPL", "instruction": "BUY"})
            ledger.record("signal", {"symbol": "MSFT"})

            signals = ledger.entries(kind="signal")
            self.assertEqual(len(signals), 2)
            self.assertTrue(all(e["kind"] == "signal" for e in signals))

            fills = ledger.entries(kind="fill")
            self.assertEqual(len(fills), 1)

    def test_ledger_summary(self):
        """Test ledger summary."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "trades.jsonl"
            ledger = Ledger(str(ledger_path))

            ledger.record("signal", {"symbol": "AAPL"})
            ledger.record("signal", {"symbol": "MSFT"})
            ledger.record("fill", {"symbol": "AAPL", "instruction": "BUY", "notional": 1500.0})
            ledger.record("fill", {"symbol": "MSFT", "instruction": "SELL", "notional": 2000.0})

            summary = ledger.summary()
            self.assertEqual(summary["kind_counts"]["signal"], 2)
            self.assertEqual(summary["kind_counts"]["fill"], 2)
            self.assertEqual(summary["buys"], 1)
            self.assertEqual(summary["sells"], 1)
            self.assertAlmostEqual(summary["buy_notional"], 1500.0)
            self.assertAlmostEqual(summary["sell_notional"], 2000.0)

    def test_ledger_empty_file(self):
        """Test ledger with no entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "trades.jsonl"
            ledger = Ledger(str(ledger_path))

            entries = ledger.entries()
            self.assertEqual(entries, [])

            summary = ledger.summary()
            self.assertEqual(summary["kind_counts"], {})


class TestBrokerClient(unittest.TestCase):
    """Test BrokerClient."""

    def test_client_url_construction(self):
        """Test URL construction."""
        client = BrokerClient("http://localhost:8788", account_id="ACC-123")
        self.assertEqual(client.base_url, "http://localhost:8788")
        self.assertEqual(client.account_id, "ACC-123")

    @patch("urllib.request.urlopen")
    def test_quotes_request(self, mock_urlopen):
        """Test quotes method URL construction."""
        response_data = json.dumps({"AAPL": {"symbol": "AAPL", "bid": 150.0, "ask": 151.0}})
        mock_urlopen.return_value.__enter__.return_value.read.return_value = response_data.encode()

        client = BrokerClient("http://localhost:8788")
        result = client.quotes(["AAPL", "MSFT"])

        # Check that urlopen was called
        self.assertTrue(mock_urlopen.called)
        call_args = mock_urlopen.call_args
        url = call_args[0][0].full_url if hasattr(call_args[0][0], "full_url") else call_args[0][0]
        self.assertIn("quotes", url)
        self.assertIn("AAPL", url)
        self.assertIn("MSFT", url)

    @patch("urllib.request.urlopen")
    def test_place_order_json_encoding(self, mock_urlopen):
        """Test place_order JSON encoding."""
        response_data = json.dumps({"orderId": "123", "status": "ACCEPTED"})
        mock_urlopen.return_value.__enter__.return_value.read.return_value = response_data.encode()

        client = BrokerClient("http://localhost:8788", account_id="ACC-123")
        result = client.place_order("AAPL", "BUY", 10, price=150.5)

        # Check request was made
        self.assertTrue(mock_urlopen.called)
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]

        # Verify POST data
        if hasattr(request_obj, "data") and request_obj.data:
            body = json.loads(request_obj.data.decode())
            self.assertEqual(body["symbol"], "AAPL")
            self.assertEqual(body["instruction"], "BUY")
            self.assertEqual(body["quantity"], 10)
            self.assertEqual(body["price"], 150.5)

    @patch("urllib.request.urlopen")
    def test_broker_error_handling(self, mock_urlopen):
        """Test error handling."""
        error_response = json.dumps({"error": "Invalid symbol"})
        mock_response = MagicMock()
        mock_response.read.return_value = error_response.encode()
        mock_urlopen.side_effect = HTTPError(
            "http://localhost:8788/v1/marketdata/quotes",
            400,
            "Bad Request",
            {},
            BytesIO(error_response.encode()),
        )

        client = BrokerClient("http://localhost:8788")

        with self.assertRaises(BrokerError) as context:
            client.quotes(["INVALID"])

        self.assertEqual(context.exception.status_code, 400)

    @patch("urllib.request.urlopen")
    def test_account_request(self, mock_urlopen):
        """Test account method."""
        response_data = json.dumps(
            {
                "accountId": "ACC-123",
                "cash": 10000.0,
                "positions": [],
                "equity": 10000.0,
                "timestamp": "2024-01-01T00:00:00Z",
            }
        )
        mock_urlopen.return_value.__enter__.return_value.read.return_value = response_data.encode()

        client = BrokerClient("http://localhost:8788", account_id="ACC-123")
        result = client.account()

        self.assertEqual(result["accountId"], "ACC-123")
        self.assertEqual(result["cash"], 10000.0)

    @patch("urllib.request.urlopen")
    def test_list_orders_empty(self, mock_urlopen):
        """Test list_orders with empty list."""
        response_data = json.dumps([])
        mock_urlopen.return_value.__enter__.return_value.read.return_value = response_data.encode()

        client = BrokerClient("http://localhost:8788")
        result = client.list_orders()

        self.assertEqual(result, [])

    @patch("urllib.request.urlopen")
    def test_list_orders_multiple(self, mock_urlopen):
        """Test list_orders with multiple orders."""
        response_data = json.dumps(
            [
                {"orderId": "1", "symbol": "AAPL", "status": "FILLED"},
                {"orderId": "2", "symbol": "MSFT", "status": "PENDING"},
            ]
        )
        mock_urlopen.return_value.__enter__.return_value.read.return_value = response_data.encode()

        client = BrokerClient("http://localhost:8788")
        result = client.list_orders()

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["symbol"], "AAPL")


class TestAlerts(unittest.TestCase):
    """Test alert generation."""

    def test_rsi_oversold_alert(self):
        """Test RSI oversold alert."""
        indicators = {"AAPL": {"rsi14": 25.0}}
        quotes = {"AAPL": {"last": 150.0}}

        alerts = compute_alerts(indicators, quotes)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "rsi_oversold")
        self.assertEqual(alerts[0]["symbol"], "AAPL")

    def test_rsi_overbought_alert(self):
        """Test RSI overbought alert."""
        indicators = {"AAPL": {"rsi14": 75.0}}
        quotes = {"AAPL": {"last": 150.0}}

        alerts = compute_alerts(indicators, quotes)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "rsi_overbought")

    def test_big_move_alert(self):
        """Test big move alert."""
        indicators = {"AAPL": {"pct_change_1d": 0.05}}
        quotes = {"AAPL": {"last": 150.0}}

        alerts = compute_alerts(indicators, quotes)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "big_move")

    def test_big_move_negative(self):
        """Test big move with negative change."""
        indicators = {"AAPL": {"pct_change_1d": -0.04}}
        quotes = {"AAPL": {"last": 150.0}}

        alerts = compute_alerts(indicators, quotes)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "big_move")

    def test_no_alert_small_move(self):
        """Test no alert for small move."""
        indicators = {"AAPL": {"pct_change_1d": 0.01}}
        quotes = {"AAPL": {"last": 150.0}}

        alerts = compute_alerts(indicators, quotes)

        self.assertEqual(len(alerts), 0)

    def test_cross_sma20_up(self):
        """Test SMA20 crossover alert (price crossing above)."""
        indicators = {"AAPL": {"sma20": 145.0, "rsi14": 50.0, "pct_change_1d": 0.01}}
        quotes = {"AAPL": {"last": 150.0}}
        prev_snapshot = {
            "quotes": {"AAPL": {"last": 144.0}},
            "indicators": {"AAPL": {"sma20": 145.0}},
        }

        alerts = compute_alerts(indicators, quotes, prev_snapshot)

        cross_alerts = [a for a in alerts if a["kind"] == "cross_sma20"]
        self.assertEqual(len(cross_alerts), 1)
        self.assertIn("above", cross_alerts[0]["detail"])

    def test_cross_sma20_down(self):
        """Test SMA20 crossover alert (price crossing below)."""
        indicators = {"AAPL": {"sma20": 145.0, "rsi14": 50.0, "pct_change_1d": -0.01}}
        quotes = {"AAPL": {"last": 140.0}}
        prev_snapshot = {
            "quotes": {"AAPL": {"last": 146.0}},
            "indicators": {"AAPL": {"sma20": 145.0}},
        }

        alerts = compute_alerts(indicators, quotes, prev_snapshot)

        cross_alerts = [a for a in alerts if a["kind"] == "cross_sma20"]
        self.assertEqual(len(cross_alerts), 1)
        self.assertIn("below", cross_alerts[0]["detail"])

    def test_no_cross_alert_same_side(self):
        """Test no alert when price stays on same side of SMA."""
        indicators = {"AAPL": {"sma20": 145.0}}
        quotes = {"AAPL": {"last": 150.0}}
        prev_snapshot = {
            "quotes": {"AAPL": {"last": 148.0}},
            "indicators": {"AAPL": {"sma20": 145.0}},
        }

        alerts = compute_alerts(indicators, quotes, prev_snapshot)

        cross_alerts = [a for a in alerts if a["kind"] == "cross_sma20"]
        self.assertEqual(len(cross_alerts), 0)

    def test_multiple_alerts_single_symbol(self):
        """Test multiple alerts for single symbol."""
        indicators = {"AAPL": {"rsi14": 28.0, "pct_change_1d": 0.05}}
        quotes = {"AAPL": {"last": 150.0}}

        alerts = compute_alerts(indicators, quotes)

        self.assertEqual(len(alerts), 2)
        kinds = {a["kind"] for a in alerts}
        self.assertIn("rsi_oversold", kinds)
        self.assertIn("big_move", kinds)

    def test_alerts_multiple_symbols(self):
        """Test alerts across multiple symbols."""
        indicators = {"AAPL": {"rsi14": 28.0}, "MSFT": {"rsi14": 75.0}}
        quotes = {"AAPL": {"last": 150.0}, "MSFT": {"last": 300.0}}

        alerts = compute_alerts(indicators, quotes)

        self.assertEqual(len(alerts), 2)
        symbols = {a["symbol"] for a in alerts}
        self.assertIn("AAPL", symbols)
        self.assertIn("MSFT", symbols)


class TestNews(unittest.TestCase):
    """Test news functionality."""

    def test_sentiment_score_positive(self):
        """Test sentiment_score for positive keywords."""
        result = sentiment_score("Stock rises to new highs")
        self.assertEqual(result, 1)

    def test_sentiment_score_negative(self):
        """Test sentiment_score for negative keywords."""
        result = sentiment_score("Stock falls sharply")
        self.assertEqual(result, -1)

    def test_sentiment_score_neutral(self):
        """Test sentiment_score for neutral headline."""
        result = sentiment_score("Company reports earnings")
        self.assertEqual(result, 0)

    def test_sentiment_score_mixed_equal(self):
        """Test sentiment_score with equal positive and negative keywords."""
        result = sentiment_score("Stock beat but drop today")
        self.assertEqual(result, 0)

    def test_sentiment_score_multiple_positive(self):
        """Test sentiment_score with multiple positive keywords."""
        result = sentiment_score("Stock rises and beats expectations, upgrade coming")
        self.assertEqual(result, 1)

    def test_sentiment_score_multiple_negative(self):
        """Test sentiment_score with multiple negative keywords."""
        result = sentiment_score("Stock falls and disappoints, downgrade expected")
        self.assertEqual(result, -1)

    def test_sentiment_score_case_insensitive(self):
        """Test sentiment_score case insensitivity."""
        result1 = sentiment_score("Stock RISES")
        result2 = sentiment_score("Stock rises")
        self.assertEqual(result1, result2)
        self.assertEqual(result1, 1)

    def test_summarize_news_empty(self):
        """Test summarize_news with empty news."""
        news = {}
        result = summarize_news(news)
        self.assertEqual(result, {})

    def test_summarize_news_empty_items(self):
        """Test summarize_news with empty items list."""
        news = {"AAPL": []}
        result = summarize_news(news)
        self.assertIn("AAPL", result)
        self.assertEqual(result["AAPL"]["count"], 0)
        self.assertEqual(result["AAPL"]["wire_count"], 0)
        self.assertEqual(result["AAPL"]["board_count"], 0)
        self.assertEqual(result["AAPL"]["wire_sentiment"], 0)
        self.assertEqual(result["AAPL"]["board_sentiment"], 0)
        self.assertIsNone(result["AAPL"]["latest_headline"])

    def test_summarize_news_single_symbol(self):
        """Test summarize_news with single symbol."""
        news = {
            "AAPL": [
                {"id": "1", "symbol": "AAPL", "source": "wire", "headline": "Stock rises", "published": "2024-01-01"},
                {"id": "2", "symbol": "AAPL", "source": "board", "headline": "Disappointing news", "published": "2024-01-02"},
            ]
        }

        result = summarize_news(news)
        self.assertIn("AAPL", result)
        self.assertEqual(result["AAPL"]["count"], 2)
        self.assertEqual(result["AAPL"]["wire_count"], 1)
        self.assertEqual(result["AAPL"]["board_count"], 1)
        self.assertEqual(result["AAPL"]["wire_sentiment"], 1)
        self.assertEqual(result["AAPL"]["board_sentiment"], -1)
        self.assertEqual(result["AAPL"]["latest_headline"], "Stock rises")

    def test_summarize_news_multiple_symbols(self):
        """Test summarize_news with multiple symbols."""
        news = {
            "AAPL": [
                {"id": "1", "symbol": "AAPL", "source": "wire", "headline": "Stock rises", "published": "2024-01-01"},
            ],
            "MSFT": [
                {"id": "2", "symbol": "MSFT", "source": "board", "headline": "Disappointing news", "published": "2024-01-02"},
            ],
        }

        result = summarize_news(news)
        self.assertEqual(len(result), 2)
        self.assertIn("AAPL", result)
        self.assertIn("MSFT", result)

    def test_news_burst_alert(self):
        """Test news_burst alert when count >= 4."""
        news = {
            "AAPL": [
                {"id": "1", "symbol": "AAPL", "source": "wire", "headline": "News 1", "published": "2024-01-01"},
                {"id": "2", "symbol": "AAPL", "source": "wire", "headline": "News 2", "published": "2024-01-02"},
                {"id": "3", "symbol": "AAPL", "source": "board", "headline": "News 3", "published": "2024-01-03"},
                {"id": "4", "symbol": "AAPL", "source": "board", "headline": "News 4", "published": "2024-01-04"},
            ]
        }

        indicators = {"AAPL": {}}
        quotes = {"AAPL": {}}

        alerts = compute_alerts(indicators, quotes, news=news)

        burst_alerts = [a for a in alerts if a["kind"] == "news_burst"]
        self.assertEqual(len(burst_alerts), 1)
        self.assertEqual(burst_alerts[0]["symbol"], "AAPL")
        self.assertIn("count=4", burst_alerts[0]["detail"])

    def test_news_burst_alert_threshold(self):
        """Test no news_burst alert when count < 4."""
        news = {
            "AAPL": [
                {"id": "1", "symbol": "AAPL", "source": "wire", "headline": "News 1", "published": "2024-01-01"},
                {"id": "2", "symbol": "AAPL", "source": "wire", "headline": "News 2", "published": "2024-01-02"},
                {"id": "3", "symbol": "AAPL", "source": "board", "headline": "News 3", "published": "2024-01-03"},
            ]
        }

        indicators = {"AAPL": {}}
        quotes = {"AAPL": {}}

        alerts = compute_alerts(indicators, quotes, news=news)

        burst_alerts = [a for a in alerts if a["kind"] == "news_burst"]
        self.assertEqual(len(burst_alerts), 0)

    def test_sentiment_divergence_alert(self):
        """Test sentiment_divergence alert when wire and board sentiments disagree."""
        news = {
            "AAPL": [
                {"id": "1", "symbol": "AAPL", "source": "wire", "headline": "Stock rises", "published": "2024-01-01"},
                {"id": "2", "symbol": "AAPL", "source": "board", "headline": "Stock falls", "published": "2024-01-02"},
            ]
        }

        indicators = {"AAPL": {}}
        quotes = {"AAPL": {}}

        alerts = compute_alerts(indicators, quotes, news=news)

        div_alerts = [a for a in alerts if a["kind"] == "sentiment_divergence"]
        self.assertEqual(len(div_alerts), 1)
        self.assertEqual(div_alerts[0]["symbol"], "AAPL")
        self.assertIn("disagree", div_alerts[0]["detail"])

    def test_sentiment_divergence_no_alert_same_sign(self):
        """Test no sentiment_divergence alert when sentiments agree."""
        news = {
            "AAPL": [
                {"id": "1", "symbol": "AAPL", "source": "wire", "headline": "Stock rises", "published": "2024-01-01"},
                {"id": "2", "symbol": "AAPL", "source": "board", "headline": "Stock rises", "published": "2024-01-02"},
            ]
        }

        indicators = {"AAPL": {}}
        quotes = {"AAPL": {}}

        alerts = compute_alerts(indicators, quotes, news=news)

        div_alerts = [a for a in alerts if a["kind"] == "sentiment_divergence"]
        self.assertEqual(len(div_alerts), 0)

    def test_sentiment_divergence_no_alert_one_zero(self):
        """Test no sentiment_divergence alert when one sentiment is zero."""
        news = {
            "AAPL": [
                {"id": "1", "symbol": "AAPL", "source": "wire", "headline": "Stock rises", "published": "2024-01-01"},
                {"id": "2", "symbol": "AAPL", "source": "board", "headline": "No news here", "published": "2024-01-02"},
            ]
        }

        indicators = {"AAPL": {}}
        quotes = {"AAPL": {}}

        alerts = compute_alerts(indicators, quotes, news=news)

        div_alerts = [a for a in alerts if a["kind"] == "sentiment_divergence"]
        self.assertEqual(len(div_alerts), 0)

    @patch("urllib.request.urlopen")
    def test_client_news_url_construction(self, mock_urlopen):
        """Test client.news URL construction."""
        response_data = json.dumps({"AAPL": [], "MSFT": []})
        mock_urlopen.return_value.__enter__.return_value.read.return_value = response_data.encode()

        client = BrokerClient("http://localhost:8788")
        result = client.news(["AAPL", "MSFT"], limit=10)

        self.assertTrue(mock_urlopen.called)
        call_args = mock_urlopen.call_args
        url = call_args[0][0].full_url if hasattr(call_args[0][0], "full_url") else str(call_args[0][0])
        self.assertIn("news", url)
        self.assertIn("AAPL", url)
        self.assertIn("MSFT", url)
        self.assertIn("limit=10", url)

    @patch("urllib.request.urlopen")
    def test_client_news_default_limit(self, mock_urlopen):
        """Test client.news with default limit."""
        response_data = json.dumps({"AAPL": []})
        mock_urlopen.return_value.__enter__.return_value.read.return_value = response_data.encode()

        client = BrokerClient("http://localhost:8788")
        result = client.news(["AAPL"])

        self.assertTrue(mock_urlopen.called)
        call_args = mock_urlopen.call_args
        url = call_args[0][0].full_url if hasattr(call_args[0][0], "full_url") else str(call_args[0][0])
        self.assertIn("limit=10", url)


if __name__ == "__main__":
    unittest.main()
