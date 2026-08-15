"""Technical indicators for analysis."""

from typing import Any
from math import sqrt


def sma(closes: list[float], n: int) -> float | None:
    """Simple moving average."""
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def ema(closes: list[float], n: int) -> float | None:
    """Exponential moving average."""
    if len(closes) < n:
        return None

    multiplier = 2.0 / (n + 1)
    ema_val = sum(closes[:n]) / n

    for i in range(n, len(closes)):
        ema_val = (closes[i] * multiplier) + (ema_val * (1 - multiplier))

    return ema_val


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Relative Strength Index (Wilder's smoothing)."""
    if len(closes) < n + 1:
        return None

    gains = []
    losses = []

    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n

    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 0.0

    rs = avg_gain / avg_loss
    rsi_val = 100 - (100 / (1 + rs))

    return rsi_val


def atr(candles: list[dict], n: int = 14) -> float | None:
    """Average True Range."""
    if len(candles) < n:
        return None

    true_ranges = []

    for i, candle in enumerate(candles):
        high = candle.get("high", 0)
        low = candle.get("low", 0)

        if i == 0:
            tr = high - low
        else:
            prev_close = candles[i - 1].get("close", 0)
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))

        true_ranges.append(tr)

    atr_val = sum(true_ranges[-n:]) / n

    return atr_val


def pct_change(closes: list[float], n: int) -> float | None:
    """Percentage change over n periods."""
    if len(closes) < n + 1:
        return None
    if closes[-(n + 1)] == 0:
        return None
    return (closes[-1] - closes[-(n + 1)]) / closes[-(n + 1)]


def vwap(candles: list[dict]) -> float | None:
    """Volume-weighted average price: sum(typical_price * volume) / sum(volume)."""
    if not candles:
        return None

    total_volume = sum(c.get("volume", 0) for c in candles)
    if total_volume == 0:
        return None

    weighted_sum = sum(
        ((c.get("high", 0) + c.get("low", 0) + c.get("close", 0)) / 3) * c.get("volume", 0)
        for c in candles
    )

    return weighted_sum / total_volume


def latest_day(candles: list[dict]) -> list[dict]:
    """Return only the candles whose datetime falls on the most recent calendar date."""
    if not candles:
        return []

    # Get the date part (first 10 chars: YYYY-MM-DD) from the last candle
    last_datetime = candles[-1].get("datetime", "")
    if not last_datetime or len(last_datetime) < 10:
        return []

    latest_date = last_datetime[:10]

    # Find all candles with the same date
    result = []
    for candle in candles:
        candle_datetime = candle.get("datetime", "")
        if candle_datetime and len(candle_datetime) >= 10:
            if candle_datetime[:10] == latest_date:
                result.append(candle)

    return result


def opening_range(candles: list[dict], bars: int = 6) -> dict | None:
    """Get high and low of the first N bars of the latest day."""
    day_candles = latest_day(candles)
    if not day_candles:
        return None

    first_bars = day_candles[:bars]
    if not first_bars:
        return None

    highs = [c.get("high", 0) for c in first_bars]
    lows = [c.get("low", 0) for c in first_bars]

    return {
        "high": max(highs),
        "low": min(lows),
    }


def day_stats(candles: list[dict]) -> dict | None:
    """Get daily open, high, low, and vwap for the latest day."""
    day_candles = latest_day(candles)
    if not day_candles:
        return None

    opens = [c.get("open", 0) for c in day_candles]
    highs = [c.get("high", 0) for c in day_candles]
    lows = [c.get("low", 0) for c in day_candles]

    return {
        "open": opens[0] if opens else None,
        "high": max(highs) if highs else None,
        "low": min(lows) if lows else None,
        "vwap": vwap(day_candles),
    }


def _population_stdev(values: list[float]) -> float:
    """Population standard deviation."""
    if len(values) == 0:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    return sqrt(variance)


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD (Moving Average Convergence Divergence)."""
    if len(closes) < slow + signal:
        return None

    # Compute EMA fast and slow series from index slow-1 onwards
    ema_fast_series = []
    ema_slow_series = []

    # Initialize EMAs
    ema_fast_val = sum(closes[:fast]) / fast
    ema_slow_val = sum(closes[:slow]) / slow

    multiplier_fast = 2.0 / (fast + 1)
    multiplier_slow = 2.0 / (slow + 1)

    # Compute series from index slow-1 onwards
    for i in range(slow, len(closes)):
        ema_fast_val = (closes[i] * multiplier_fast) + (ema_fast_val * (1 - multiplier_fast))
        ema_slow_val = (closes[i] * multiplier_slow) + (ema_slow_val * (1 - multiplier_slow))
        ema_fast_series.append(ema_fast_val)
        ema_slow_series.append(ema_slow_val)

    # MACD line series
    macd_series = [f - s for f, s in zip(ema_fast_series, ema_slow_series)]

    if len(macd_series) < signal:
        return None

    # Signal line is EMA of MACD series
    signal_val = sum(macd_series[:signal]) / signal
    multiplier_signal = 2.0 / (signal + 1)

    for i in range(signal, len(macd_series)):
        signal_val = (macd_series[i] * multiplier_signal) + (signal_val * (1 - multiplier_signal))

    macd_val = macd_series[-1]
    hist = macd_val - signal_val

    return {
        "macd": macd_val,
        "signal": signal_val,
        "hist": hist,
    }


def stochastic(candles: list[dict], k: int = 14, d: int = 3) -> dict | None:
    """Stochastic Oscillator."""
    if len(candles) < k + d - 1:
        return None

    # Compute %K series
    k_series = []
    for i in range(k - 1, len(candles)):
        window = candles[i - k + 1 : i + 1]
        highs = [c.get("high", 0) for c in window]
        lows = [c.get("low", 0) for c in window]
        close = candles[i].get("close", 0)

        highest = max(highs)
        lowest = min(lows)
        range_val = highest - lowest

        if range_val == 0:
            k_val = 0.0
        else:
            k_val = 100.0 * (close - lowest) / range_val

        k_series.append(k_val)

    if len(k_series) < d:
        return None

    # %D is SMA of last d %K values
    d_val = sum(k_series[-d:]) / d

    return {
        "k": k_series[-1],
        "d": d_val,
    }


def adx(candles: list[dict], n: int = 14) -> dict | None:
    """Average Directional Index (Wilder's smoothing)."""
    if len(candles) < 2 * n + 1:
        return None

    # Compute +DM, -DM, TR for each bar
    plus_dm_series = []
    minus_dm_series = []
    tr_series = []

    for i, candle in enumerate(candles):
        high = candle.get("high", 0)
        low = candle.get("low", 0)

        if i == 0:
            tr = high - low
            plus_dm = 0.0
            minus_dm = 0.0
        else:
            prev_close = candles[i - 1].get("close", 0)
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))

            high_diff = high - candles[i - 1].get("high", 0)
            low_diff = candles[i - 1].get("low", 0) - low

            plus_dm = 0.0
            minus_dm = 0.0

            if high_diff > low_diff and high_diff > 0:
                plus_dm = high_diff
            if low_diff > high_diff and low_diff > 0:
                minus_dm = low_diff

        plus_dm_series.append(plus_dm)
        minus_dm_series.append(minus_dm)
        tr_series.append(tr)

    # Wilder's smoothing: first value is sum of n periods, then smoothed
    plus_dm_smoothed = sum(plus_dm_series[1 : n + 1])
    minus_dm_smoothed = sum(minus_dm_series[1 : n + 1])
    tr_smoothed = sum(tr_series[:n])

    # Continue smoothing for the rest of the series
    for i in range(n + 1, len(candles)):
        plus_dm_smoothed = plus_dm_smoothed - (plus_dm_smoothed / n) + plus_dm_series[i]
        minus_dm_smoothed = minus_dm_smoothed - (minus_dm_smoothed / n) + minus_dm_series[i]
        tr_smoothed = tr_smoothed - (tr_smoothed / n) + tr_series[i]

    # Calculate DI+ and DI-
    if tr_smoothed == 0:
        plus_di = 0.0
        minus_di = 0.0
    else:
        plus_di = 100.0 * plus_dm_smoothed / tr_smoothed
        minus_di = 100.0 * minus_dm_smoothed / tr_smoothed

    # Calculate DX and ADX
    di_sum = plus_di + minus_di
    if di_sum == 0:
        dx = 0.0
    else:
        dx = 100.0 * abs(plus_di - minus_di) / di_sum

    # ADX is Wilder's average of DX over n periods
    # Compute DX series from bar n onwards
    dx_series = []
    plus_dm_smoothed = sum(plus_dm_series[1 : n + 1])
    minus_dm_smoothed = sum(minus_dm_series[1 : n + 1])
    tr_smoothed = sum(tr_series[:n])

    for i in range(n, len(candles)):
        if i > n:
            plus_dm_smoothed = plus_dm_smoothed - (plus_dm_smoothed / n) + plus_dm_series[i]
            minus_dm_smoothed = minus_dm_smoothed - (minus_dm_smoothed / n) + minus_dm_series[i]
            tr_smoothed = tr_smoothed - (tr_smoothed / n) + tr_series[i]

        if tr_smoothed == 0:
            plus_di_i = 0.0
            minus_di_i = 0.0
        else:
            plus_di_i = 100.0 * plus_dm_smoothed / tr_smoothed
            minus_di_i = 100.0 * minus_dm_smoothed / tr_smoothed

        di_sum_i = plus_di_i + minus_di_i
        if di_sum_i == 0:
            dx_i = 0.0
        else:
            dx_i = 100.0 * abs(plus_di_i - minus_di_i) / di_sum_i

        dx_series.append(dx_i)

    if len(dx_series) < n:
        return None

    # Wilder's smoothed ADX
    adx_val = sum(dx_series[:n]) / n
    for i in range(n, len(dx_series)):
        adx_val = (adx_val * (n - 1) + dx_series[i]) / n

    return {
        "adx": adx_val,
        "plus_di": plus_di,
        "minus_di": minus_di,
    }


def roc(closes: list[float], n: int = 10) -> float | None:
    """Rate of Change: 100 * (close / close[n periods ago] - 1)."""
    if len(closes) < n + 1:
        return None

    prev_close = closes[-(n + 1)]
    if prev_close == 0:
        return None

    return 100.0 * (closes[-1] / prev_close - 1.0)


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> dict | None:
    """Bollinger Bands."""
    if len(closes) < n:
        return None

    mid = sma(closes, n)
    if mid is None:
        return None

    last_n = closes[-n:]
    sd = _population_stdev(last_n)

    upper = mid + k * sd
    lower = mid - k * sd

    # Bandwidth: (upper - lower) / mid
    if mid == 0:
        bandwidth = 0.0
    else:
        bandwidth = (upper - lower) / mid

    # Percent B: (close - lower) / (upper - lower)
    range_val = upper - lower
    if range_val == 0:
        percent_b = 0.5
    else:
        percent_b = (closes[-1] - lower) / range_val

    return {
        "upper": upper,
        "lower": lower,
        "mid": mid,
        "bandwidth": bandwidth,
        "percent_b": percent_b,
    }


def obv(candles: list[dict]) -> float | None:
    """On-Balance Volume."""
    if len(candles) < 2:
        return None

    obv_val = 0.0

    for i in range(len(candles)):
        volume = candles[i].get("volume", 0)
        close = candles[i].get("close", 0)

        if i == 0:
            obv_val = volume
        else:
            prev_close = candles[i - 1].get("close", 0)
            if close > prev_close:
                obv_val += volume
            elif close < prev_close:
                obv_val -= volume

    return obv_val


def relative_volume(candles: list[dict], bars: int = 6) -> float | None:
    """Relative Volume: mean volume of last N bars / mean volume of all earlier candles."""
    if len(candles) <= bars:
        return None

    recent_volumes = [c.get("volume", 0) for c in candles[-bars:]]
    earlier_volumes = [c.get("volume", 0) for c in candles[:-bars]]

    if not earlier_volumes:
        return None

    recent_mean = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0
    earlier_mean = sum(earlier_volumes) / len(earlier_volumes) if earlier_volumes else 0

    if earlier_mean == 0:
        return None

    return recent_mean / earlier_mean


def summarize(candles: list[dict]) -> dict:
    """Compute technical summary from candles."""
    if not candles:
        return {
            "last_close": None,
            "sma20": None,
            "ema9": None,
            "rsi14": None,
            "atr14": None,
            "pct_change_1d": None,
            "pct_change_5d": None,
            "vwap": None,
            "day_open": None,
            "day_high": None,
            "day_low": None,
            "range_high": None,
            "range_low": None,
            "macd_hist": None,
            "stoch_k": None,
            "stoch_d": None,
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "roc10": None,
            "bb_percent_b": None,
            "bb_bandwidth": None,
            "bb_upper": None,
            "bb_lower": None,
            "rel_volume": None,
        }

    closes = [c.get("close", 0) for c in candles]

    last_close = closes[-1] if closes else None
    sma20_val = sma(closes, 20)
    ema9_val = ema(closes, 9)
    rsi14_val = rsi(closes, 14)
    atr14_val = atr(candles, 14)

    # pct_change_1d: last vs 78 5-min bars back (1d = 24*60/5 = 288 bars, but clamp to available)
    # Actually, 1 day of 5-min candles is 288 candles. Let's use 78 to match the spec's "78 5-min bars back"
    pct_change_1d = pct_change(closes, min(78, len(closes) - 1)) if len(closes) > 1 else None

    # pct_change_5d: last vs 5 days back (5*288 = 1440 bars, but clamp)
    pct_change_5d = pct_change(closes, min(len(closes) - 1, len(closes) - 1))

    # New intraday indicators
    day_stats_val = day_stats(candles)
    vwap_val = day_stats_val["vwap"] if day_stats_val else None
    day_open_val = day_stats_val["open"] if day_stats_val else None
    day_high_val = day_stats_val["high"] if day_stats_val else None
    day_low_val = day_stats_val["low"] if day_stats_val else None

    opening_range_val = opening_range(candles, bars=6)
    range_high_val = opening_range_val["high"] if opening_range_val else None
    range_low_val = opening_range_val["low"] if opening_range_val else None

    # Momentum indicators
    macd_val = macd(closes)
    macd_hist = macd_val["hist"] if macd_val else None

    stoch_val = stochastic(candles)
    stoch_k = stoch_val["k"] if stoch_val else None
    stoch_d = stoch_val["d"] if stoch_val else None

    adx_val = adx(candles)
    adx_result = adx_val["adx"] if adx_val else None
    plus_di = adx_val["plus_di"] if adx_val else None
    minus_di = adx_val["minus_di"] if adx_val else None

    roc10_val = roc(closes, 10)

    bb_val = bollinger(closes, 20)
    bb_percent_b = bb_val["percent_b"] if bb_val else None
    bb_bandwidth = bb_val["bandwidth"] if bb_val else None
    bb_upper = bb_val["upper"] if bb_val else None
    bb_lower = bb_val["lower"] if bb_val else None

    rel_volume = relative_volume(candles, 6)

    return {
        "last_close": last_close,
        "sma20": sma20_val,
        "ema9": ema9_val,
        "rsi14": rsi14_val,
        "atr14": atr14_val,
        "pct_change_1d": pct_change_1d,
        "pct_change_5d": pct_change_5d,
        "vwap": vwap_val,
        "day_open": day_open_val,
        "day_high": day_high_val,
        "day_low": day_low_val,
        "range_high": range_high_val,
        "range_low": range_low_val,
        "macd_hist": macd_hist,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "adx": adx_result,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "roc10": roc10_val,
        "bb_percent_b": bb_percent_b,
        "bb_bandwidth": bb_bandwidth,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "rel_volume": rel_volume,
    }
