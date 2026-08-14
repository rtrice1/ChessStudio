"""HTTP server for mock Schwab API."""

import argparse
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from .accounts import AccountEngine
from .market import MarketSim
from .news import NewsFeed


# Global server state
_market: Optional[MarketSim] = None
_engine: Optional[AccountEngine] = None
_news_feed: Optional[NewsFeed] = None
_engine_lock = threading.Lock()


class SchwabRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for mock Schwab API."""

    def do_GET(self) -> None:
        """Handle GET requests."""
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query_string = urllib.parse.parse_qs(parsed_path.query)

        with _engine_lock:
            if path == "/v1/marketdata/quotes":
                self._handle_quotes(query_string)
            elif path == "/v1/marketdata/pricehistory":
                self._handle_price_history(query_string)
            elif path == "/v1/marketdata/news":
                self._handle_news(query_string)
            elif path.startswith("/v1/accounts/"):
                self._handle_account_get(path, query_string)
            else:
                self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        """Handle POST requests."""
        path = self.path
        content_length = int(self.headers.get("Content-Length", 0))
        body_data = self.rfile.read(content_length)

        try:
            body = json.loads(body_data.decode("utf-8")) if body_data else {}
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        with _engine_lock:
            if path.startswith("/v1/accounts/") and path.endswith("/orders"):
                self._handle_place_order(path, body)
            else:
                self.send_error(404, "Not Found")

    def do_DELETE(self) -> None:
        """Handle DELETE requests."""
        path = self.path

        with _engine_lock:
            if path.startswith("/v1/accounts/") and "/orders/" in path:
                self._handle_cancel_order(path)
            else:
                self.send_error(404, "Not Found")

    def _handle_quotes(self, query_string: dict) -> None:
        """GET /v1/marketdata/quotes?symbols=AAPL,MSFT"""
        symbols_param = query_string.get("symbols", [""])
        symbols = symbols_param[0].split(",") if symbols_param[0] else []

        if not symbols:
            self.send_error(400, "Missing symbols parameter")
            return

        result = {}
        for symbol in symbols:
            quote = _market.quote(symbol)
            result[symbol] = quote

        self._send_json(200, result)

    def _handle_price_history(self, query_string: dict) -> None:
        """GET /v1/marketdata/pricehistory?symbol=AAPL&days=5&interval=5"""
        symbol = query_string.get("symbol", [""])[0]
        days_str = query_string.get("days", ["5"])[0]
        interval_str = query_string.get("interval", ["60"])[0]

        if not symbol:
            self.send_error(400, "Missing symbol parameter")
            return

        try:
            days = int(days_str)
            interval = int(interval_str)
        except ValueError:
            self.send_error(400, "Invalid days or interval")
            return

        history = _market.price_history(symbol, days=days, interval_minutes=interval)

        if "error" in history:
            self.send_error(400, history["error"])
        else:
            self._send_json(200, history)

    def _handle_news(self, query_string: dict) -> None:
        """GET /v1/marketdata/news?symbols=AAPL,MSFT&limit=10"""
        symbols_param = query_string.get("symbols", [""])
        symbols = symbols_param[0].split(",") if symbols_param[0] else []
        limit_str = query_string.get("limit", ["10"])[0]

        try:
            limit = int(limit_str)
        except ValueError:
            limit = 10

        result = {}
        for symbol in symbols:
            if symbol:
                result[symbol] = _news_feed.items(symbol, limit)
            else:
                result[symbol] = []

        self._send_json(200, result)

    def _handle_account_get(self, path: str, query_string: dict) -> None:
        """
        Handle GET requests for account info.
        Paths:
          /v1/accounts/{accountId}
          /v1/accounts/{accountId}/orders
          /v1/accounts/{accountId}/orders/{orderId}
        """
        parts = path.strip("/").split("/")

        if len(parts) < 3:
            self.send_error(404, "Not Found")
            return

        account_id = parts[2]
        if account_id != _engine.account_id:
            self.send_error(404, "Account not found")
            return

        if len(parts) == 3:
            # GET /v1/accounts/{accountId}
            _engine.process_open_orders()
            snapshot = _engine.snapshot()
            self._send_json(200, snapshot)

        elif len(parts) == 4 and parts[3] == "orders":
            # GET /v1/accounts/{accountId}/orders
            orders = _engine.list_orders()
            self._send_json(200, {"orders": orders})

        elif len(parts) == 5 and parts[3] == "orders":
            # GET /v1/accounts/{accountId}/orders/{orderId}
            order_id = parts[4]
            order = _engine.get_order(order_id)
            if order is None:
                self.send_error(404, "Order not found")
            else:
                self._send_json(200, order)

        else:
            self.send_error(404, "Not Found")

    def _handle_place_order(self, path: str, body: dict) -> None:
        """POST /v1/accounts/{accountId}/orders"""
        parts = path.strip("/").split("/")
        account_id = parts[2]

        if account_id != _engine.account_id:
            self.send_error(404, "Account not found")
            return

        # Extract order parameters
        symbol = body.get("symbol")
        instruction = body.get("instruction")
        quantity = body.get("quantity")
        order_type = body.get("orderType")
        price = body.get("price")

        if not all([symbol, instruction, quantity, order_type]):
            self.send_error(400, "Missing required fields")
            return

        try:
            quantity = int(quantity)
        except (ValueError, TypeError):
            self.send_error(400, "Invalid quantity")
            return

        if price is not None:
            try:
                price = float(price)
            except (ValueError, TypeError):
                self.send_error(400, "Invalid price")
                return

        order = _engine.place_order(symbol, instruction, quantity, order_type, price)

        if order.get("status") == "REJECTED":
            self._send_json(400, order)
        else:
            self._send_json(201, order)

    def _handle_cancel_order(self, path: str) -> None:
        """DELETE /v1/accounts/{accountId}/orders/{orderId}"""
        parts = path.strip("/").split("/")
        if len(parts) < 5:
            self.send_error(404, "Not Found")
            return

        account_id = parts[2]
        order_id = parts[4]

        if account_id != _engine.account_id:
            self.send_error(404, "Account not found")
            return

        result = _engine.cancel_order(order_id)
        if "error" in result:
            status_code = 400 if "Cannot cancel" in result["error"] else 404
            self._send_json(status_code, result)
        else:
            self._send_json(200, result)

    def _send_json(self, status_code: int, data: dict) -> None:
        """Send JSON response."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


def create_server(
    host: str = "127.0.0.1",
    port: int = 8788,
    seed: int = 42,
    time_scale: float = 1.0,
    starting_cash: float = 100_000.0,
) -> ThreadingHTTPServer:
    """
    Create and initialize the HTTP server.

    Args:
        host: Server host.
        port: Server port.
        seed: Random seed for market simulator.
        time_scale: Simulation time acceleration factor.
        starting_cash: Initial account cash.

    Returns:
        ThreadingHTTPServer instance.
    """
    global _market, _engine, _news_feed

    _market = MarketSim(seed=seed, time_scale=time_scale)
    _engine = AccountEngine(_market, starting_cash=starting_cash)
    _news_feed = NewsFeed(_market, seed=seed)

    server = ThreadingHTTPServer((host, port), SchwabRequestHandler)
    return server


def main() -> None:
    """Main entry point with argument parsing."""
    parser = argparse.ArgumentParser(description="Mock Schwab API server")
    parser.add_argument("--host", default="127.0.0.1", help="Server host")
    parser.add_argument("--port", type=int, default=8788, help="Server port")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for market simulator")
    parser.add_argument("--time-scale", type=float, default=1.0, help="Simulation time scale")
    parser.add_argument(
        "--starting-cash", type=float, default=100_000.0, help="Initial account cash"
    )

    args = parser.parse_args()

    server = create_server(
        host=args.host,
        port=args.port,
        seed=args.seed,
        time_scale=args.time_scale,
        starting_cash=args.starting_cash,
    )

    print(f"Starting Mock Schwab API server on {args.host}:{args.port}")
    print(f"  Seed: {args.seed}")
    print(f"  Time scale: {args.time_scale}")
    print(f"  Starting cash: ${args.starting_cash:,.2f}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
