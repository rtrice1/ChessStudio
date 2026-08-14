"""Day taxonomy — what kind of day is this turning into?

Experienced day traders insist days fit patterns: the violent 09:30–10:00
followed by a settling-in, the one-way trend day, the chop that stops
everyone out both directions, the opening spike that fades all afternoon.
This module turns that folk taxonomy into a measurable fingerprint so it
can be classified while the day develops, journaled when the day ends,
and matched against remembered days (see agent/gut.py).

Features (aggregated across the watchlist's current-day candles):
- open_vol_ratio: first-30-min bar volatility vs. the rest of the day so
  far. High = the open was the event.
- efficiency: net move / total path traveled. Trend days are efficient;
  chop travels far and goes nowhere.
- breadth: fraction of symbols up on the day. Trend days agree; mixed
  days don't.
- vwap_above_frac: how persistently price holds one side of VWAP.
- avg_abs_return: how big the day is, regardless of shape.
"""
from __future__ import annotations

import statistics

from .indicators import latest_day, vwap

OPEN_BARS = 6  # first 30 minutes of 5-minute bars


def _symbol_features(candles: list[dict]) -> dict | None:
    day = latest_day(candles)
    if len(day) < OPEN_BARS + 4:
        return None
    closes = [float(c["close"]) for c in day]
    opens_ = float(day[0]["open"])

    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]
    open_rets, rest_rets = rets[:OPEN_BARS - 1], rets[OPEN_BARS - 1:]
    if len(open_rets) < 2 or len(rest_rets) < 2:
        return None
    open_vol = statistics.pstdev(open_rets)
    rest_vol = statistics.pstdev(rest_rets)

    path = sum(abs(r) for r in rets)
    net = (closes[-1] - opens_) / opens_
    day_vwap = vwap(day)
    return {
        "open_vol_ratio": open_vol / rest_vol if rest_vol > 0 else 3.0,
        "efficiency": abs(net) / path if path > 0 else 0.0,
        "day_return": net,
        "vwap_above_frac": (sum(1 for c in closes if day_vwap and c > day_vwap)
                            / len(closes)) if day_vwap else 0.5,
    }


def day_features(candles_by_symbol: dict) -> dict | None:
    """Aggregate per-symbol fingerprints into one day fingerprint."""
    per_symbol = [f for f in (_symbol_features(c) for c in candles_by_symbol.values())
                  if f is not None]
    if not per_symbol:
        return None
    n = len(per_symbol)
    return {
        "open_vol_ratio": sum(f["open_vol_ratio"] for f in per_symbol) / n,
        "efficiency": sum(f["efficiency"] for f in per_symbol) / n,
        "breadth": sum(1 for f in per_symbol if f["day_return"] > 0) / n,
        "avg_abs_return": sum(abs(f["day_return"]) for f in per_symbol) / n,
        "vwap_above_frac": sum(f["vwap_above_frac"] for f in per_symbol) / n,
        "n_symbols": n,
    }


# Thresholds are CALIBRATED TO THE MARKET THEY LIVE IN — currently set at
# empirical percentiles of 200 simulated days (efficiency p50=0.19 p90=0.25;
# open_vol_ratio p90=1.13; breadth p10/p90=0.3/0.7). A day type is a
# relative statement ("more one-way than most days"), so on real data these
# get re-derived from real percentiles, not reused. Note the sim has no
# opening-auction dynamics — its "open_spike_settle" tags the top decile of
# open-vs-day volatility, a pale shadow of the real 09:30 violence.
TREND_EFFICIENCY = 0.24       # ~p85
SPIKE_OPEN_VOL_RATIO = 1.15   # ~p90
CHOP_EFFICIENCY = 0.15        # ~p15
BREADTH_HI, BREADTH_LO = 0.7, 0.3


def classify_day(features: dict) -> dict:
    """Map a fingerprint to the folk taxonomy. Deliberately coarse — the
    point is a stable label the gut can accumulate history against, not a
    precise model."""
    f = features
    eff, breadth, ovr = f["efficiency"], f["breadth"], f["open_vol_ratio"]

    if eff > TREND_EFFICIENCY and breadth >= BREADTH_HI:
        day_type, confidence = "trend_up", min(0.9, 0.5 + eff)
    elif eff > TREND_EFFICIENCY and breadth <= BREADTH_LO:
        day_type, confidence = "trend_down", min(0.9, 0.5 + eff)
    elif ovr > SPIKE_OPEN_VOL_RATIO and eff <= TREND_EFFICIENCY:
        # the classic: violent open, then the day settles in
        day_type, confidence = "open_spike_settle", min(0.85, 0.4 + ovr / 4)
    elif eff < CHOP_EFFICIENCY:
        day_type, confidence = "chop", 0.6
    else:
        day_type, confidence = "mixed", 0.3
    return {"day_type": day_type, "confidence": round(confidence, 2),
            "features": features}


def features_from_client(client, symbols: list[str]) -> dict | None:
    """Fetch current-day candles and fingerprint the developing day."""
    candles = {}
    for symbol in symbols:
        try:
            candles[symbol] = client.price_history(symbol, days=1, interval=5)["candles"]
        except Exception:
            continue
    return day_features(candles) if candles else None
