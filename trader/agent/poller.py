"""Market data poller."""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .client import BrokerClient, BrokerError
from .indicators import summarize


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def sentiment_score(headline: str) -> int:
    """
    Compute sentiment score from headline.
    Returns +1 for positive, -1 for negative, 0 for neutral.
    Uses crude keyword lists with case-insensitive matching.
    """
    positive = ["rise", "rises", "surge", "beat", "beats", "upbeat", "raise", "bullish", "moon", "loading", "brewing", "breakout", "upgrade"]
    negative = ["slip", "slips", "fall", "falls", "drop", "cut", "cuts", "disappointing", "bearish", "cooked", "bag", "coping", "downgrade", "weighs", "plunge"]

    headline_lower = headline.lower()

    pos_count = sum(1 for word in positive if word in headline_lower)
    neg_count = sum(1 for word in negative if word in headline_lower)

    if pos_count > neg_count:
        return 1
    elif neg_count > pos_count:
        return -1
    else:
        return 0


def mention_velocity(items: list[dict]) -> dict:
    """Board mention rate-of-change: posts in the last hour vs the hour
    before. The sentiment is misleading by construction; the *acceleration*
    is honest — a name going from 2 posts/hour to 20 predicts volatility
    (not direction), and that's exactly what a day trader wants to know."""
    from datetime import datetime as _dt

    stamps = []
    for item in items:
        if item.get("source") != "board":
            continue
        try:
            stamps.append(_dt.fromisoformat(str(item.get("ts", ""))))
        except ValueError:
            continue
    if not stamps:
        return {"recent": 0, "prior": 0, "ratio": None, "accel": None}
    now = max(stamps)
    hour = 3600.0
    recent = sum(1 for s in stamps if (now - s).total_seconds() <= hour)
    prior = sum(1 for s in stamps
                if hour < (now - s).total_seconds() <= 2 * hour)
    prior2 = sum(1 for s in stamps
                 if 2 * hour < (now - s).total_seconds() <= 3 * hour)
    ratio = (recent / prior) if prior else None
    # Discrete second difference of the mention counts. Only meaningful
    # once the window depth exists (a capped 10-item mock feed rarely has
    # it) — None until all three windows have any history behind them.
    accel = (recent - 2 * prior + prior2) if (prior or prior2) else None
    return {"recent": recent, "prior": prior, "ratio": ratio, "accel": accel}


def summarize_news(news: dict) -> dict:
    """
    Summarize news across symbols.
    Returns per-symbol: count, wire_count, board_count, wire_sentiment, board_sentiment, latest_headline.
    """
    summary = {}
    for symbol, items in news.items():
        if not items:
            summary[symbol] = {
                "count": 0,
                "wire_count": 0,
                "board_count": 0,
                "wire_sentiment": 0,
                "board_sentiment": 0,
                "board_velocity": None,
                "board_recent": 0,
                "latest_headline": None,
            }
            continue

        wire_items = [item for item in items if item.get("source") == "wire"]
        board_items = [item for item in items if item.get("source") == "board"]

        wire_sentiment = sum(sentiment_score(item.get("headline", "")) for item in wire_items)
        board_sentiment = sum(sentiment_score(item.get("headline", "")) for item in board_items)

        velocity = mention_velocity(items)
        summary[symbol] = {
            "count": len(items),
            "wire_count": len(wire_items),
            "board_count": len(board_items),
            "wire_sentiment": wire_sentiment,
            "board_sentiment": board_sentiment,
            "board_velocity": velocity["ratio"],
            "board_recent": velocity["recent"],
            "latest_headline": items[0].get("headline") if items else None,
        }

    return summary


def compute_alerts(
    indicators: dict[str, dict],
    quotes: dict[str, dict],
    prev_snapshot: dict | None = None,
    news: dict | None = None,
) -> list[dict]:
    """Compute alerts from indicators, quotes, previous snapshot, and news."""
    alerts = []

    # Halted names: the last print is frozen fiction; flag it loudly.
    for symbol, quote in quotes.items():
        if quote and quote.get("halted"):
            alerts.append({"symbol": symbol, "kind": "halted",
                           "detail": "trading halted — quotes are stale"})

    for symbol, summary in indicators.items():
        if summary is None:
            continue

        # RSI alerts
        rsi14 = summary.get("rsi14")
        if rsi14 is not None:
            if rsi14 < 30:
                alerts.append({"symbol": symbol, "kind": "rsi_oversold", "detail": f"RSI14={rsi14:.2f}"})
            elif rsi14 > 70:
                alerts.append(
                    {"symbol": symbol, "kind": "rsi_overbought", "detail": f"RSI14={rsi14:.2f}"}
                )

        # Big move alert
        pct_change_1d = summary.get("pct_change_1d")
        if pct_change_1d is not None and abs(pct_change_1d) > 0.03:
            alerts.append(
                {
                    "symbol": symbol,
                    "kind": "big_move",
                    "detail": f"pct_change_1d={pct_change_1d * 100:.2f}%",
                }
            )

        # Cross SMA20 alert
        if prev_snapshot is not None:
            current_price = quotes.get(symbol, {}).get("last")
            sma20 = summary.get("sma20")

            if current_price is not None and sma20 is not None:
                prev_indicators = prev_snapshot.get("indicators", {}).get(symbol)
                if prev_indicators:
                    prev_price = prev_snapshot.get("quotes", {}).get(symbol, {}).get("last")
                    prev_sma20 = prev_indicators.get("sma20")

                    if prev_price is not None and prev_sma20 is not None:
                        # Check if price crossed SMA20
                        if (prev_price < prev_sma20 and current_price > sma20) or (
                            prev_price > prev_sma20 and current_price < sma20
                        ):
                            direction = "above" if current_price > sma20 else "below"
                            alerts.append(
                                {
                                    "symbol": symbol,
                                    "kind": "cross_sma20",
                                    "detail": f"Crossed {direction} SMA20 ({sma20:.2f})",
                                }
                            )

    # News alerts
    if news:
        news_summary = summarize_news(news)
        for symbol, summary in news_summary.items():
            # News burst alert
            if summary["count"] >= 4:
                alerts.append({
                    "symbol": symbol,
                    "kind": "news_burst",
                    "detail": f"count={summary['count']}, wire_sentiment={summary['wire_sentiment']}, board_sentiment={summary['board_sentiment']}",
                })

            # Sentiment divergence alert
            wire_sent = summary["wire_sentiment"]
            board_sent = summary["board_sentiment"]
            if wire_sent != 0 and board_sent != 0 and (wire_sent > 0) != (board_sent > 0):
                alerts.append({
                    "symbol": symbol,
                    "kind": "sentiment_divergence",
                    "detail": "wire and board disagree — someone is wrong",
                })

    return alerts


def poll_once(client: BrokerClient, symbols: list[str], data_dir: str) -> dict:
    """Poll market data once and write snapshot."""
    try:
        # Fetch quotes
        quotes_data = client.quotes(symbols)

        # Fetch price history and compute indicators
        indicators = {}
        for symbol in symbols:
            try:
                history = client.price_history(symbol, days=5, interval=5)
                candles = history.get("candles", [])
                indicators[symbol] = summarize(candles)
            except Exception as e:
                logger.error(f"Error fetching history for {symbol}: {e}")
                indicators[symbol] = None

        # Fetch account
        account_data = client.account()

        # Fetch news
        news = {}
        try:
            news = client.news(symbols, limit=10)
        except Exception as e:
            logger.warning(f"Error fetching news: {e}")
            news = {}

        # Load previous snapshot if available for alert detection
        prev_snapshot = None
        latest_path = Path(data_dir) / "latest.json"
        if latest_path.exists():
            try:
                with open(latest_path) as f:
                    prev_snapshot = json.load(f)
            except Exception as e:
                logger.warning(f"Could not load previous snapshot: {e}")

        # Compute alerts
        alerts = compute_alerts(indicators, quotes_data, prev_snapshot, news)

        # Build snapshot
        timestamp = datetime.now(timezone.utc).isoformat()
        news_summary = summarize_news(news)
        snapshot = {
            "timestamp": timestamp,
            "account": account_data,
            "quotes": quotes_data,
            "indicators": indicators,
            "alerts": alerts,
            "news": {
                "items": news,
                "summary": news_summary,
            },
        }

        # Create snapshots directory
        snapshots_dir = Path(data_dir) / "snapshots"
        snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Write snapshot file
        dt_utc = datetime.now(timezone.utc)
        snapshot_filename = dt_utc.strftime("%Y%m%d-%H%M%S.json")
        snapshot_path = snapshots_dir / snapshot_filename

        with open(snapshot_path, "w") as f:
            json.dump(snapshot, f)

        # Write latest.json atomically
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = latest_path.with_suffix(".tmp")
        with open(temp_path, "w") as f:
            json.dump(snapshot, f)
        os.replace(temp_path, latest_path)

        return snapshot

    except Exception as e:
        logger.error(f"Poll error: {e}")
        raise


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Market data poller")
    parser.add_argument("--base-url", default="http://127.0.0.1:8788")
    parser.add_argument("--account", default="PAPER-001")
    parser.add_argument(
        "--symbols", default="AAPL,MSFT,NVDA,GOOGL,AMZN,SPY,QQQ,TSLA,JPM,XOM"
    )
    parser.add_argument("--data-dir", default="trader/data")
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--iterations", type=int, default=0)

    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    client = BrokerClient(args.base_url, account_id=args.account)

    iteration = 0
    while args.iterations == 0 or iteration < args.iterations:
        try:
            snapshot = poll_once(client, symbols, args.data_dir)
            equity = snapshot.get("account", {}).get("equity", 0)
            alerts_count = len(snapshot.get("alerts", []))
            print(
                f"[{snapshot['timestamp']}] equity={equity:.2f}, alerts={alerts_count}, symbols={len(symbols)}"
            )
        except Exception as e:
            logger.error(f"Poll iteration {iteration} failed: {e}")

        if args.interval_seconds == 0:
            break

        iteration += 1
        if args.iterations > 0 and iteration >= args.iterations:
            break

        time.sleep(args.interval_seconds)


if __name__ == "__main__":
    main()
