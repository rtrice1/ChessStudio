"""Technical indicators for analysis."""

from typing import Any


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
    }
