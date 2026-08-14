"""News impact measurement without look-ahead.

The rule, straight from the human partner: stock movements may only be
analyzed *after* the news is released. It sounds obvious and is the most
commonly violated rule in backtesting — a headline timestamped 14:00
"explaining" a move that happened at 13:40 teaches a model to trade on
information from the future, and the lesson evaporates the moment it
trades for real.

So this module is the only sanctioned way to relate news to prices:
every measurement anchors at the first candle at-or-after the item's
`published` timestamp and looks strictly forward. Candles before
publication are structurally excluded — there is no parameter that
admits them. If the news is so fresh no candle exists after it yet, the
answer is None: wait, don't peek.
"""
from __future__ import annotations

from datetime import datetime


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def forward_return(candles: list[dict], published_iso: str,
                   bars_after: int = 6) -> dict | None:
    """Price reaction following a publication time.

    Anchors at the first candle whose datetime >= published; the baseline
    is that candle's OPEN (the first tradeable price after the news
    existed). Measures to the close bars_after candles later, clipped to
    what exists. Returns None when no candle follows publication, or when
    nothing but the anchor candle exists yet.
    """
    published = _parse(published_iso)
    anchor = None
    for i, candle in enumerate(candles):
        if _parse(candle["datetime"]) >= published:
            anchor = i
            break
    if anchor is None:
        return None  # news newer than all price data: wait, don't peek

    end = min(anchor + bars_after, len(candles) - 1)
    if end <= anchor and len(candles) - 1 == anchor:
        # only the anchor bar exists so far; a 0-bar "reaction" is noise
        return None
    baseline = float(candles[anchor]["open"])
    if baseline <= 0:
        return None
    final = float(candles[end]["close"])
    return {
        "anchor_time": candles[anchor]["datetime"],
        "baseline": baseline,
        "bars_used": end - anchor,
        "ret": (final - baseline) / baseline,
    }


def news_impact(items: list[dict], candles: list[dict],
                bars_after: int = 6) -> list[dict]:
    """Forward-only reaction for each news item against one symbol's
    candles. Items whose reaction can't be measured yet carry
    forward=None rather than a peeked number."""
    out = []
    for item in items:
        out.append({
            "id": item.get("id"),
            "symbol": item.get("symbol"),
            "source": item.get("source"),
            "headline": item.get("headline"),
            "published": item.get("published"),
            "forward": forward_return(candles, item["published"], bars_after),
        })
    return out


def scoreboard(measured: list[dict]) -> dict:
    """Aggregate measured impacts by source — the beginning of an answer
    to 'is the news predictive, contrarian, or noise?', built only from
    forward-looking measurements."""
    by_source: dict[str, list[float]] = {}
    for m in measured:
        if m.get("forward"):
            by_source.setdefault(m.get("source") or "?", []).append(
                m["forward"]["ret"])
    return {
        source: {"n": len(rets),
                 "mean_ret": sum(rets) / len(rets),
                 "hit_rate_up": sum(1 for r in rets if r > 0) / len(rets)}
        for source, rets in by_source.items() if rets
    }
