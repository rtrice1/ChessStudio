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


def compute_alerts(
    indicators: dict[str, dict],
    quotes: dict[str, dict],
    prev_snapshot: dict | None = None,
) -> list[dict]:
    """Compute alerts from indicators, quotes, and previous snapshot."""
    alerts = []

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
        alerts = compute_alerts(indicators, quotes_data, prev_snapshot)

        # Build snapshot
        timestamp = datetime.now(timezone.utc).isoformat()
        snapshot = {
            "timestamp": timestamp,
            "account": account_data,
            "quotes": quotes_data,
            "indicators": indicators,
            "alerts": alerts,
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
