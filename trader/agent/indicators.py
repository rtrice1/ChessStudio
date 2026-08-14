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

    return {
        "last_close": last_close,
        "sma20": sma20_val,
        "ema9": ema9_val,
        "rsi14": rsi14_val,
        "atr14": atr14_val,
        "pct_change_1d": pct_change_1d,
        "pct_change_5d": pct_change_5d,
    }
